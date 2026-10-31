"""Run with unittest on a Python containing OpenCV; no web dependencies required."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

try:
    import cv2
    import numpy as np
    HAS_CV = hasattr(cv2, 'aruco')
except ImportError:
    HAS_CV = False

if HAS_CV:
    spec = importlib.util.spec_from_file_location('lens_cv', Path(__file__).parents[1] / 'reco_field' / 'lens_cv.py')
    lens = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lens)


@unittest.skipUnless(HAS_CV, 'OpenCV aruco unavailable in this Python')
class LensCvTests(unittest.TestCase):
    def synthetic_views(self, *, flat=False):
        size = (1280, 960)
        k = np.array([[610., 0, 635.], [0, 605., 486.], [0, 0, 1.]])
        d = np.array([-.025, .004, -.0008, .0001])
        world = lens.board().getChessboardCorners().astype(np.float64)
        rng = np.random.default_rng(83)
        views = []
        for index, (x, y, depth) in enumerate((x, y, z) for z in (.28, .4, .55)
                                            for y in (.17, .5, .83) for x in (.17, .5, .83)):
            rotation = np.zeros(3) if flat else np.array([.3 * (-1) ** index, .4 * (-1) ** (index // 2), .12 * (-1) ** (index // 3)])
            matrix, _ = cv2.Rodrigues(rotation)
            ray = cv2.fisheye.undistortPoints(np.array([[[x * size[0], y * size[1]]]]), k, d).reshape(2)
            translation = np.array([ray[0] * depth, ray[1] * depth, depth]) - matrix @ np.array([.105, .075, 0])
            points, _ = cv2.fisheye.projectPoints(world.reshape(-1, 1, 3), rotation, translation, k, d)
            points = points.reshape(-1, 2) + rng.normal(0, .015, (len(world), 2))
            valid = np.all((points >= [0, 0]) & (points < size), axis=1)
            if valid.sum() >= 12:
                ids = np.flatnonzero(valid).tolist()
                views.append({'ids': ids, 'points': points[valid].tolist(),
                    'descriptor': lens.view_descriptor(points[valid], ids, size)})
        return views, size, k, d

    def test_recovers_fisheye_intrinsics_and_validates_holdout(self):
        views, size, k, _ = self.synthetic_views()
        result = lens.solve(views, size)
        profile = result['profile']
        self.assertLess(abs(profile['fx'] / k[0, 0] - 1), .01)
        self.assertLess(abs(profile['fy'] / k[1, 1] - 1), .01)
        self.assertLess(abs(profile['cx'] - k[0, 2]), 2)
        self.assertLess(abs(profile['cy'] - k[1, 2]), 2)
        self.assertEqual(len(profile['d']), 4)
        self.assertLess(result['quality']['holdout_rms_px'], .1)
        self.assertGreaterEqual(result['quality']['holdout_views'], 4)

    def test_repeated_central_views_are_rejected(self):
        views, size, _, _ = self.synthetic_views()
        central = min(views, key=lambda v: abs(v['descriptor'][0] - .5) + abs(v['descriptor'][1] - .5))
        with self.assertRaisesRegex(ValueError, 'cover enough'):
            lens.solve([central] * 20, size)

    def test_front_parallel_views_need_more_tilt(self):
        views, size, _, _ = self.synthetic_views(flat=True)
        with self.assertRaisesRegex(ValueError, 'variety|Lens solve failed'):
            lens.solve(views, size)

    def test_heldout_errors_reject_an_otherwise_good_training_fit(self):
        views, size, _, _ = self.synthetic_views()
        for index, view in enumerate(views):
            if index % 5 == 0:
                points = np.array(view['points'])
                points[:, 0] += np.where(np.arange(len(points)) % 2, 7, -7)
                view['points'] = points.tolist()
        with self.assertRaisesRegex(ValueError, 'Fit rejected'):
            lens.solve(views, size)

    def test_outliers_and_invalid_points_are_rejected(self):
        views, size, _, _ = self.synthetic_views()
        views[0]['points'][0][0] = float('nan')
        with self.assertRaisesRegex(ValueError, 'Invalid board'):
            lens.solve(views, size)
        views, size, _, _ = self.synthetic_views()
        rng = np.random.default_rng(91)
        for view in views:
            view['points'] = (np.asarray(view['points']) + rng.normal(0, 5, (len(view['ids']), 2))).tolist()
        with self.assertRaises(ValueError):
            lens.solve(views, size)

    def test_printed_pattern_is_detected_at_full_size(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'board.png'
            image = np.full((1700, 2300), 255, np.uint8)
            image[100:1600, 100:2200] = lens.board().generateImage((2100, 1500))
            cv2.imwrite(str(path), image)
            observation = lens.detect(path)
            self.assertEqual(observation['size'], [2300, 1700])
            self.assertEqual(len(observation['ids']), 24)
            svg = lens.printable_board()
            self.assertIn('width="230mm" height="170mm"', svg)


if __name__ == '__main__':
    unittest.main()
