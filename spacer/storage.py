"""
Defines storage ABC; implementations; and factory.
"""

import abc
import logging
import os
import pickle
import warnings
from functools import lru_cache
from io import BytesIO
from pickle import Unpickler
from typing import Union, Tuple
from urllib.error import URLError

import botocore.exceptions
import wget
from PIL import Image
from sklearn.calibration import CalibratedClassifierCV

from spacer import config
from spacer.exceptions import SpacerInputError


class Storage(abc.ABC):  # pragma: no cover

    @abc.abstractmethod
    def store(self, key: str, stream: BytesIO) -> None:
        """ Stores a BytesIO stream """

    @abc.abstractmethod
    def load(self, key: str) -> BytesIO:
        """ Loads key to BytesIO stream """

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        """ Deletes key if it exists """

    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        """ Checks if key exists """


class URLStorage(Storage):
    """ Loads items from URLs. Does not support store operations. """

    def __init__(self):
        self.fs_storage = FileSystemStorage()

    def store(self, url: str, stream: BytesIO):
        raise TypeError('Store operation not supported for URL storage.')

    def load(self, url: str):
        try:
            tmp_path = wget.download(url, out=config.TMP_PATH, bar=None)
        except URLError as e:
            raise SpacerInputError(str(e))
        stream = self.fs_storage.load(tmp_path)
        self.fs_storage.delete(tmp_path)
        return stream

    def delete(self, url: str) -> None:
        raise TypeError('Delete operation not supported for URL storage.')

    def exists(self, url: str) -> bool:
        try:
            tmp_path = wget.download(url, out=config.TMP_PATH, bar=None)
        except URLError:
            return False
        except ValueError:
            return False
        self.fs_storage.delete(tmp_path)
        return True


class S3Storage(Storage):
    """ Stores objects on AWS S3 """

    def __init__(self, bucketname: str):
        self.bucketname = bucketname

    def store(self, key: str, stream: BytesIO):
        s3 = config.get_s3_conn()
        s3.Bucket(self.bucketname).put_object(Body=stream, Key=key)

    def load(self, key: str):
        s3 = config.get_s3_conn()
        stream = BytesIO()
        s3.Object(self.bucketname, key).download_fileobj(stream)
        return stream

    def delete(self, key: str) -> None:
        s3 = config.get_s3_conn()
        s3.Object(self.bucketname, key).delete()

    def exists(self, key: str):
        s3 = config.get_s3_conn()
        try:
            s3.Object(self.bucketname, key).load()
            return True
        except botocore.exceptions.ClientError:
            return False


class FileSystemStorage(Storage):
    """ Stores objects on disk """

    def __init__(self):
        pass

    def store(self, key: str, stream: BytesIO):

        dirname = os.path.dirname(os.path.abspath(key))
        if not os.path.exists(dirname):
            os.makedirs(dirname)
        with open(key, 'wb') as f:
            f.write(stream.getbuffer())

    def load(self, key: str):
        with open(key, 'rb') as f:
            return BytesIO(f.read())

    def delete(self, path: str):
        os.remove(path)

    def exists(self, path: str):
        return os.path.exists(path)


class MemoryStorage(Storage):
    """ This stores objects in RAM. Useful for testing only. """

    def __init__(self):
        self.blobs = {}

    def store(self, key: str, stream: BytesIO):
        self.blobs[key] = stream.getvalue()

    def load(self, key: str):
        stream = BytesIO(self.blobs[key])
        return stream

    def delete(self, path: str):
        del self.blobs[path]

    def exists(self, path: str):
        return path in self.blobs


# This holds the global memory storage.
_memorystorage = None


def clear_memory_storage():
    """ Clears global memory storage"""
    global _memorystorage
    _memorystorage = None


def storage_factory(storage_type: str, bucketname: Union[str, None] = None):

    assert storage_type in config.STORAGE_TYPES

    if storage_type == 's3':
        return S3Storage(bucketname=bucketname)
    if storage_type == 'filesystem':
        return FileSystemStorage()
    if storage_type == 'memory':
        global _memorystorage
        if _memorystorage is None:
            _memorystorage = MemoryStorage()
        return _memorystorage
    if storage_type == 'url':
        return URLStorage()


def download_model(keyname: str) -> Tuple[str, bool]:
    """
    Utility method to download model to local cache.
    This is not part of the storage interface since it is a special case,
    and models need to be downloaded to a specific destination folder to be
    shared with host filesystem.
    """
    assert config.HAS_S3_MODEL_ACCESS, "Need access to model bucket."
    assert config.HAS_LOCAL_MODEL_PATH, "Model path not set or is invalid."
    destination = os.path.join(config.LOCAL_MODEL_PATH, keyname)

    with config.log_entry_and_exit('fetching of model to ' + destination):
        if not os.path.isfile(destination):
            with config.log_entry_and_exit('downloading from ' + keyname):
                s3 = config.get_s3_conn()
                s3.Bucket(config.MODELS_BUCKET).download_file(keyname,
                                                              destination)
                was_cashed = False
        else:
            # Already cached, no need to download
            was_cashed = True

    return destination, was_cashed


def store_image(loc: 'DataLocation', img: Image):
    storage = storage_factory(loc.storage_type, loc.bucket_name)
    with BytesIO() as stream:
        img.save(stream, 'JPEG')
        stream.seek(0)
        storage.store(loc.key, stream)


def load_image(loc: 'DataLocation'):
    storage = storage_factory(loc.storage_type, loc.bucket_name)
    return Image.open(storage.load(loc.key))


def store_classifier(loc: 'DataLocation', clf: CalibratedClassifierCV):
    if not hasattr(clf, 'calibrated_classifiers_'):
        raise ValueError("Only fitted classifiers can be stored.")
    storage = storage_factory(loc.storage_type, loc.bucket_name)
    storage.store(loc.key, BytesIO(pickle.dumps(clf, protocol=2)))


class ClassifierUnpickler(Unpickler):
    """
    Custom Unpickler for sklearn classifiers. Upgrades classifiers pickled
    in older sklearn versions to be loadable in newer versions.
    pyspacer has used scikit-learn versions 0.17.1, 0.22.1, and 1.1.3,
    so these are the only versions that are considered.

    Note: this is only tested for inference.
    """
    IMPORT_MAPPING = {
        # Importing from most sklearn sub-modules was deprecated as
        # of 0.22 and no longer possible as of 0.24 (they established an API
        # deprecation cycle of two minor versions starting in 0.22).
        'sklearn.linear_model.sgd_fast': 'sklearn.linear_model',
        'sklearn.linear_model.stochastic_gradient': 'sklearn.linear_model',
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # We can't see the base class's attributes from here for some reason,
        # so we have to set anything we want ourselves.
        if 'fix_imports' in kwargs:
            self.fix_imports = kwargs['fix_imports']

    def find_class(self, module, name):
        if self.fix_imports:
            if module in self.IMPORT_MAPPING:
                module = self.IMPORT_MAPPING[module]
        return super().find_class(module, name)

    def load(self):
        clf = super().load()

        # Detect legacy classifiers and patch as needed.
        # Most of the relevant CalibratedClassifierCV attributes seem to be
        # unchanged from 0.17.1 to 1.1.3, except for calibrated_classifiers_,
        # which is a list of _CalibratedClassifier instances.
        for calibrated_clf in clf.calibrated_classifiers_:

            # This attribute was introduced after 0.17.1 and by 0.22.1. It's set
            # unconditionally in __init__().
            if not hasattr(calibrated_clf, 'classes'):
                calibrated_clf.classes = calibrated_clf.classes_

            # This attribute was introduced after 0.22.1 and by 0.24.2. It's set
            # unconditionally in __init__().
            if not hasattr(calibrated_clf, 'calibrators'):
                calibrated_clf.calibrators = calibrated_clf.calibrators_

        return clf


@lru_cache(maxsize=3)
def load_classifier(loc: 'DataLocation'):

    storage = storage_factory(loc.storage_type, loc.bucket_name)
    stream = storage.load(loc.key)
    stream.seek(0)

    # Restore old warnings config once this block is over.
    with warnings.catch_warnings():
        # Ignore unpickling warnings from sklearn.
        warnings.filterwarnings(
            'ignore', category=UserWarning,
            message=r"Trying to unpickle estimator [A-Za-z_]+"
                    r" from version pre-0\.18.*")
        clf = ClassifierUnpickler(
            stream, fix_imports=True, encoding='latin1').load()

    return clf
