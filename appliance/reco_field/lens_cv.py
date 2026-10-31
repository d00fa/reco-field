"""Bounded OpenCV worker; invoked with the system Python, outside the app venv."""
from __future__ import annotations

import base64
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

SQUARES = (7, 5)
SQUARE_METRES = 0.030
MIN_VIEWS = 16
MAX_VIEWS = 32


def board():
    return cv2.aruco.CharucoBoard(SQUARES, SQUARE_METRES, 0.022,
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50))


def printable_board() -> str:
    image = board().generateImage((2100, 1500), marginSize=0, borderBits=1)
    ok, encoded = cv2.imencode('.png', image)
    if not ok:
        raise ValueError('Cannot generate calibration board.')
    data = base64.b64encode(encoded).decode('ascii')
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="230mm" height="170mm" '
        'viewBox="0 0 230 170"><rect width="230" height="170" fill="white"/>'
        f'<image x="10" y="10" width="210" height="150" href="data:image/png;base64,{data}"/>'
        '<text x="10" y="167" font-family="sans-serif" font-size="3">'
        'Reco Field: 30 mm squares. Print at 100%, no fit-to-page. Check with a ruler.</text></svg>')


def view_descriptor(points, ids, size):
    points = np.asarray(points, np.float64).reshape(-1, 2)
    width, height = size
    norm = points / np.array([width, height])
    world = board().getChessboardCorners()[np.asarray(ids).reshape(-1), :2]
    homography, _ = cv2.findHomography(world.astype(np.float64), norm, 0)
    if homography is None or not np.isfinite(homography).all():
        raise ValueError('Board corners are degenerate. Tilt the board and try again.')
    homography /= homography[2, 2]
    return [*norm.mean(axis=0).tolist(), float(np.sqrt(np.ptp(norm[:, 0]) * np.ptp(norm[:, 1]))),
            float(homography[2, 0] * .21), float(homography[2, 1] * .15)]


def detect(image_path: Path) -> dict:
    if image_path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('Calibration image exceeds 32 MiB.')
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError('Camera did not produce a readable calibration image.')
    height, width = image.shape
    if not (320 <= width <= 8192 and 180 <= height <= 8192 and width * height <= 34_000_000):
        raise ValueError('Calibration image dimensions are unsupported.')
    corners, ids, _, _ = cv2.aruco.CharucoDetector(board()).detectBoard(image)
    count = 0 if ids is None else len(ids)
    if count < 12:
        raise ValueError(f'Found {count}/24 board corners; need at least 12. Show more board and improve focus/light.')
    points = corners.reshape(-1, 2)
    if np.ptp(points[:, 0]) * np.ptp(points[:, 1]) < width * height * .008:
        raise ValueError('Board is too small. Move it closer while keeping at least half visible.')
    return {'size': [width, height], 'ids': ids.reshape(-1).tolist(),
            'points': points.tolist(), 'descriptor': view_descriptor(points, ids, [width, height])}


def validate_views(views, size, *, minimum=MIN_VIEWS):
    if not isinstance(views, list) or not minimum <= len(views) <= MAX_VIEWS:
        raise ValueError(f'Collect {MIN_VIEWS}–{MAX_VIEWS} varied board views before solving.')
    width, height = size
    if any(isinstance(v, bool) or not isinstance(v, int) for v in size) or not (320 <= width <= 8192 and 180 <= height <= 8192):
        raise ValueError('Invalid calibration image size.')
    all_points, centres = [], []
    for view in views:
        ids = np.asarray(view['ids'])
        points = np.asarray(view['points'], dtype=np.float64)
        if (ids.ndim != 1 or not 12 <= len(ids) <= 24 or points.shape != (len(ids), 2)
            or not np.issubdtype(ids.dtype, np.integer) or len(set(ids.tolist())) != len(ids)
            or np.any(ids < 0) or np.any(ids >= 24) or not np.isfinite(points).all()
            or np.any(points < 0) or np.any(points >= [width, height])):
            raise ValueError('Invalid board-corner observations.')
        norm = points / [width, height]
        all_points.append(norm)
        centres.append(norm.mean(axis=0))
    points = np.concatenate(all_points)
    centres = np.asarray(centres)
    cells = set(map(tuple, np.minimum((points * 3).astype(int), 2)))
    coverage = np.ptp(points, axis=0)
    if (len(cells) < 6 or coverage[0] < .7 or coverage[1] < .6
        or np.any(np.ptp(centres, axis=0) < .25)):
        raise ValueError('Views do not cover enough of the lens. Capture the board near all four edges and at different tilts/distances.')
    return {'grid_cells': len(cells), 'width_fraction': float(coverage[0]),
            'height_fraction': float(coverage[1])}


def solve(views, size) -> dict:
    coverage = validate_views(views, size)
    width, height = size
    world = board().getChessboardCorners().astype(np.float64)
    objects = [world[np.asarray(v['ids'], int)].reshape(-1, 1, 3) for v in views]
    images = [np.asarray(v['points'], np.float64).reshape(-1, 1, 2) for v in views]
    holdout = [i for i in range(len(views)) if i % 5 == 0]
    training = [i for i in range(len(views)) if i not in holdout]
    k = np.array([[width * .5, 0, width * .5], [0, width * .5, height * .5], [0, 0, 1]], np.float64)
    d = np.zeros((4, 1), np.float64)
    flags = (cv2.fisheye.CALIB_USE_INTRINSIC_GUESS | cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC
             | cv2.fisheye.CALIB_CHECK_COND | cv2.fisheye.CALIB_FIX_SKEW)
    try:
        rms, k, d, rotations, translations = cv2.fisheye.calibrate([objects[i] for i in training],
            [images[i] for i in training], (width, height), k, d, flags=flags,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 150, 1e-8))
    except cv2.error as exc:
        raise ValueError('Lens solve failed. Add sharper views with different tilts, distances and edge coverage.') from exc
    normals = np.array([cv2.Rodrigues(r)[0][:, 2] for r in rotations])
    tilt_span = math.degrees(math.acos(float(np.clip(np.min(normals @ normals.T), -1, 1))))
    depths = np.array([t[2, 0] for t in translations])
    if np.min(depths) <= 0 or tilt_span < 15 or np.max(depths) / np.min(depths) < 1.15:
        raise ValueError('Views need more variety. Tilt the board in both directions and change its distance from the lens.')
    if (not np.isfinite(k).all() or not np.isfinite(d).all() or not math.isfinite(rms)
        or not .1 * width < k[0, 0] < 3 * width or not .1 * width < k[1, 1] < 3 * width
        or not 0 < k[0, 2] < width or not 0 < k[1, 2] < height):
        raise ValueError('Lens fit produced implausible camera parameters.')
    errors, observed_angles = [], []
    for index in holdout:
        normalized = cv2.fisheye.undistortPoints(images[index], k, d)
        ok, rotation, translation = cv2.solvePnP(objects[index], normalized, np.eye(3), None)
        if not ok or translation[2, 0] <= 0:
            raise ValueError('Cannot validate the lens against held-out views.')
        projected, _ = cv2.fisheye.projectPoints(objects[index], rotation, translation, k, d)
        residual = projected.reshape(-1, 2) - images[index].reshape(-1, 2)
        errors.append(float(np.sqrt(np.mean(np.sum(residual * residual, axis=1)))))
        observed_angles.extend(np.arctan(np.linalg.norm(normalized.reshape(-1, 2), axis=1)))
    theta = np.linspace(0, min(max(observed_angles) * 1.1, 1.55), 100)
    coefficients = d.reshape(4)
    derivative = 1 + sum((2 * i + 3) * coefficients[i] * theta ** (2 * i + 2) for i in range(4))
    if np.min(derivative) <= 0:
        raise ValueError('Lens distortion folds within the measured view. Collect better edge coverage.')
    holdout_rms = float(np.sqrt(np.mean(np.square(errors))))
    if rms > 1.5 or holdout_rms > 2.0 or max(errors) > 3.0:
        raise ValueError(f'Fit rejected: training {rms:.2f}px, held-out {holdout_rms:.2f}px (worst {max(errors):.2f}px). Improve focus, board flatness and view variety.')
    return {'profile': {'width': width, 'height': height, 'fx': float(k[0, 0]), 'fy': float(k[1, 1]),
            'cx': float(k[0, 2]), 'cy': float(k[1, 2]), 'd': coefficients.tolist()},
            'quality': {'training_rms_px': float(rms), 'holdout_rms_px': holdout_rms,
            'holdout_max_view_rms_px': max(errors), 'training_views': len(training),
            'holdout_views': len(holdout), 'coverage': coverage, 'tilt_span_degrees': tilt_span,
            'distance_ratio': float(np.max(depths) / np.min(depths)), 'opencv_version': cv2.__version__}}


if __name__ == '__main__':
    command, source, destination = sys.argv[1:]
    output = Path(destination)
    try:
        cv2.setNumThreads(2)
        if command == 'board':
            output.write_text(printable_board(), encoding='utf-8')
        else:
            if command == 'detect':
                result = detect(Path(source))
            elif command == 'solve':
                session = json.loads(Path(source).read_text())
                result = solve(session['views'], session['size'])
            else:
                raise ValueError('Unknown lens calibration command.')
            output.write_text(json.dumps(result, allow_nan=False), encoding='utf-8')
    except (ValueError, KeyError, TypeError, OSError, cv2.error) as exc:
        output.write_text(json.dumps({'error': str(exc)[:600]}), encoding='utf-8')
        sys.exit(1)
