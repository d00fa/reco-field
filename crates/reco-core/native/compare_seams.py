#!/usr/bin/env python3
"""Compare OpenCV seam finders on one opt-in reduced diagnostic pair."""
import argparse
import json
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np


def seam_scores(left, right, valid_left, valid_right, ownership):
    both = (valid_left != 0) & (valid_right != 0)
    difference = np.abs(left.astype(np.float32) - right.astype(np.float32)).mean(axis=2)
    weights = np.zeros(both.shape, np.uint8)
    horizontal = ownership[:, :-1] != ownership[:, 1:]
    vertical = ownership[:-1, :] != ownership[1:, :]
    weights[:, :-1] += horizontal
    weights[:, 1:] += horizontal
    weights[:-1, :] += vertical
    weights[1:, :] += vertical
    weights *= both
    samples = np.repeat(difference.ravel(), weights.ravel())
    return {
        'mean_mismatch': float(samples.mean()) if samples.size else None,
        'p95_mismatch': float(np.percentile(samples, 95)) if samples.size else None,
        'boundary_pixels_in_overlap': int(samples.size),
        'changed_from_baseline_pixels': None,
    }


def compare(folder, prefix, corridors=()):
    def read(suffix, mode):
        image = cv2.imread(str(folder / (prefix + suffix)), mode)
        if image is None:
            raise ValueError('Cannot read ' + prefix + suffix)
        return image
    left = read('-left.ppm', cv2.IMREAD_COLOR)
    right = read('-right.ppm', cv2.IMREAD_COLOR)
    va = read('-left-valid.pgm', cv2.IMREAD_GRAYSCALE)
    vb = read('-right-valid.pgm', cv2.IMREAD_GRAYSCALE)
    baseline = read('-baseline.pgm', cv2.IMREAD_GRAYSCALE) >= 128
    if left.shape != right.shape or left.shape[:2] != va.shape or va.shape != vb.shape:
        raise ValueError('Diagnostic dimensions differ')
    results = {'opencv_version': cv2.__version__, 'size': [left.shape[1], left.shape[0]], 'algorithms': {}}
    results['algorithms']['baseline'] = seam_scores(left, right, va, vb, baseline)
    constructors = {
        'graphcut_color_grad': lambda: cv2.detail_GraphCutSeamFinder('COST_COLOR_GRAD'),
        'graphcut_color': lambda: cv2.detail_GraphCutSeamFinder('COST_COLOR'),
        'dp_color': lambda: cv2.detail_DpSeamFinder('COLOR'),
        'dp_color_grad': lambda: cv2.detail_DpSeamFinder('COLOR_GRAD'),
    }
    plans = [(label, constructor, va, vb) for label, constructor in constructors.items()]
    distance_right = cv2.distanceTransform(baseline.astype(np.uint8), cv2.DIST_L2, 3)
    distance_left = cv2.distanceTransform((~baseline).astype(np.uint8), cv2.DIST_L2, 3)
    for radius in corridors:
        if not 1 <= radius <= 768:
            raise ValueError('Corridor radius must be between 1 and 768 pixels')
        ca = ((va != 0) & ((~baseline) | (distance_right <= radius) | (vb == 0))).astype(np.uint8) * 255
        cb = ((vb != 0) & (baseline | (distance_left <= radius) | (va == 0))).astype(np.uint8) * 255
        plans.extend((label + '_corridor' + str(radius), constructor, ca, cb)
                     for label, constructor in constructors.items())
    for label, constructor, input_a, input_b in plans:
        start = perf_counter()
        try:
            masks = [cv2.UMat(input_a.copy()), cv2.UMat(input_b.copy())]
            images = [cv2.UMat(left.astype(np.float32)), cv2.UMat(right.astype(np.float32))]
            constructor().find(images, [(0, 0), (0, 0)], masks)
            a, b = [mask.get() != 0 for mask in masks]
            if np.any((va != 0) & (vb != 0) & ~a & ~b):
                raise ValueError('Seam leaves overlap holes')
            ownership = (vb != 0) & ((va == 0) | (b & ~a))
            scores = seam_scores(left, right, va, vb, ownership)
            scores['changed_from_baseline_pixels'] = int(np.count_nonzero(ownership != baseline))
            scores['milliseconds'] = (perf_counter() - start) * 1000
            cv2.imwrite(str(folder / (prefix + '-' + label + '.pgm')), ownership.astype(np.uint8) * 255)
            results['algorithms'][label] = scores
        except (cv2.error, ValueError) as error:
            results['algorithms'][label] = {'error': str(error), 'milliseconds': (perf_counter() - start) * 1000}
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--prefix', default='seam-1')
    parser.add_argument('--corridor', nargs='*', type=int, default=[])
    args = parser.parse_args()
    print(json.dumps(compare(args.directory, args.prefix, args.corridor), indent=2, allow_nan=False))
