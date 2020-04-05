"""
This file runs regression compared to results from the Beta production server.
See pyspacer/scripts/make_legacy_score_for_regression_testing.py for details.
"""
import json
import unittest

import numpy as np

from spacer import config
from spacer.storage import storage_factory
from spacer.messages import \
    DataLocation, \
    ExtractFeaturesMsg, \
    ClassifyFeaturesMsg, \
    ClassifyReturnMsg

from spacer.tasks import classify_features, extract_features

from spacer.data_classes import ImageFeatures

reg_meta = {
    's16': ('1355.model', ['i2921', 'i2934']),
    's295': ('10279.model', ['i1370227', 'i160100']),
    's603': ('3709.model', ['i576858', 'i576912']),
    's812': ('4772.model', ['i672762', 'i674185']),
    's1388': ('8942.model', ['i1023182', 'i1023213'])
}

s3_key_prefix = 'beta_reg/'


def get_rowcol(key, storage):
    """ This file was saved using
    coralnet/project/vision_backend/management/commands/
    vb_export_spacer_data.py

    https://github.com/beijbom/coralnet/blob/
    e08afaa0164425fc16ae4ed60841d70f2eff59a6/project/vision_backend/
    management/commands/vb_export_spacer_data.py
    """
    anns = json.loads(storage.load(key).getvalue().decode('utf-8'))
    return [(entry['row'], entry['col']) for entry in anns]


@unittest.skipUnless(config.HAS_CAFFE, 'Caffe not installed')
@unittest.skipUnless(config.HAS_S3_MODEL_ACCESS, 'No access to models')
@unittest.skipUnless(config.HAS_S3_TEST_ACCESS, 'No access to test bucket')
class TestExtractFeatures(unittest.TestCase):
    """ Tests new feature extractor against legacy.
    These tests do not pass. In tests, it varied between sources and
    images and even within images. Some row, col locations gave
    identical features while some did not. This 'test' instead
    prints the 2-norm difference.
    """

    def setUp(self):
        self.storage = storage_factory('s3', 'spacer-test')

        # Limit the number of row, col location to make tests run faster.
        self.max_rc_cnt = 2

    def run_one_test(self, im_key):
        """ Run feature extraction on an image and compare to legacy extracted
        features
        """
        new_feats_loc = DataLocation(storage_type='memory',
                                     key='features.json')

        rowcol = get_rowcol(s3_key_prefix + im_key + '.anns.json',
                            self.storage)

        msg = ExtractFeaturesMsg(
            job_token='beta_reg_test',
            feature_extractor_name='vgg16_coralnet_ver1',
            image_loc=DataLocation(storage_type='s3',
                                   bucket_name='spacer-test',
                                   key=s3_key_prefix + im_key + '.jpg'),
            rowcols=rowcol[:self.max_rc_cnt],
            feature_loc=new_feats_loc
        )
        _ = extract_features(msg)

        legacy_feats = ImageFeatures.load(
            DataLocation(
                storage_type='s3',
                bucket_name='spacer-test',
                key=s3_key_prefix + im_key + '.features.json'
            ))

        self.assertFalse(legacy_feats.valid_rowcol)
        self.assertEqual(legacy_feats.npoints, len(rowcol))
        self.assertEqual(legacy_feats.feature_dim, 4096)

        new_feats = ImageFeatures.load(new_feats_loc)

        self.assertTrue(new_feats.valid_rowcol)
        self.assertEqual(new_feats.npoints, len(msg.rowcols))
        self.assertEqual(new_feats.feature_dim, 4096)

        for legacy_pf, new_pf in zip(legacy_feats.point_features,
                                     new_feats.point_features):
            print(im_key, np.linalg.norm(np.array(legacy_pf.data) -
                                         np.array(new_pf.data)))
            with self.subTest(im_key=im_key):
                self.assertTrue(np.allclose(legacy_pf.data, new_pf.data))

    def test_all(self):

        for source, (clf, imgs) in reg_meta.items():
            for img in imgs:
                self.run_one_test(source + '/' + img)


@unittest.skipUnless(config.HAS_S3_TEST_ACCESS, 'No access to tests')
class TestClassifyFeatures(unittest.TestCase):
    """ Test the classify_features task and compare to scores
    calculated using previous sci-kit learn versions.
    Test pass if scores are identical.
    """

    def run_one_test(self, img_key, clf_key):

        msg = ClassifyFeaturesMsg(
            job_token='regression_test',
            feature_loc=DataLocation(storage_type='s3',
                                     bucket_name='spacer-test',
                                     key=s3_key_prefix + img_key +
                                         '.features.json'),
            classifier_loc=DataLocation(storage_type='s3',
                                        bucket_name='spacer-test',
                                        key=s3_key_prefix + clf_key)
        )
        new_scores = classify_features(msg)

        # The features are legacy, so the scores don't have valid row-cols.
        self.assertFalse(new_scores.valid_rowcol)

        legacy_scores = ClassifyReturnMsg.load(
            DataLocation(
                storage_type='s3',
                bucket_name='spacer-test',
                key=s3_key_prefix + img_key + '.scores.json'
            )
        )

        # The features are legacy, so the scores don't have valid row-cols.
        self.assertFalse(legacy_scores.valid_rowcol)
        for ls, ns in zip(legacy_scores.scores, new_scores.scores):
            self.assertTrue(np.allclose(ls[2], ns[2]))

    def test_all(self):

        for source, (clf, imgs) in reg_meta.items():
            for img in imgs:
                self.run_one_test(source + '/' + img, source + '/' + clf)


@unittest.skipUnless(config.HAS_CAFFE, 'Caffe not installed')
@unittest.skipUnless(config.HAS_S3_MODEL_ACCESS, 'No access to models')
@unittest.skipUnless(config.HAS_S3_TEST_ACCESS, 'No access to test bucket')
class TestFullLoop(unittest.TestCase):
    """ Tests new feature extractor and a classification against legacy.
    Test passes if the same class is assigned in both cases for each
    row, col location """

    def setUp(self):
        self.storage = storage_factory('s3', 'spacer-test')

        # Limit the number of row, col location to make tests run faster.
        self.max_rc_cnt = 10

    def run_one_test(self, im_key, clf_key):

        new_feats_loc = DataLocation(storage_type='memory',
                                     key='features.json')

        rowcol = get_rowcol(s3_key_prefix + im_key + '.anns.json',
                            self.storage)

        msg = ExtractFeaturesMsg(
            job_token='beta_reg_test',
            feature_extractor_name='vgg16_coralnet_ver1',
            image_loc=DataLocation(storage_type='s3',
                                   bucket_name='spacer-test',
                                   key=s3_key_prefix + im_key + '.jpg'),
            rowcols=rowcol[:self.max_rc_cnt],
            feature_loc=new_feats_loc
        )
        _ = extract_features(msg)

        msg = ClassifyFeaturesMsg(
            job_token='regression_test',
            feature_loc=new_feats_loc,
            classifier_loc=DataLocation(storage_type='s3',
                                        bucket_name='spacer-test',
                                        key=s3_key_prefix + clf_key)
        )
        new_scores = classify_features(msg)

        legacy_scores = ClassifyReturnMsg.load(
            DataLocation(
                storage_type='s3',
                bucket_name='spacer-test',
                key=s3_key_prefix + im_key + '.scores.json'
            )
        )
        for ls, ns in zip(legacy_scores.scores, new_scores.scores):
            print(im_key, clf_key, np.linalg.norm(np.array(ls[2]) -
                                                  np.array(ns[2])))
            with self.subTest(im_key=im_key, clf_key=clf_key):
                self.assertEqual(np.argmax(ls[2]), np.argmax(ns[2]))

    def test_all(self):

        for source, (clf, imgs) in reg_meta.items():
            for img in imgs:
                self.run_one_test(source + '/' + img, source + '/' + clf)


if __name__ == '__main__':
    unittest.main()
