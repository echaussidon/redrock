import unittest

from .. import utils

class TestUtils(unittest.TestCase):
    """Test redrock utils.
    """

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_distribute_work(self):
        nproc = 2
        ids = [0, 1, 2, 3]
        dist = utils.distribute_work(nproc, ids)
        self.assertEqual(set(map(len, dist)), {2, 2})

        weights = [3, 1, 1, 1]
        dist = utils.distribute_work(nproc, ids, weights=weights)
        self.assertEqual(set(map(len, dist)), {1, 3})

        capacities = [1, 3]
        dist = utils.distribute_work(nproc, ids, capacities=capacities)
        self.assertEqual(list(map(len, dist)), [1, 3])



def test_suite():
    """Allows testing of only this module with the command::

        python setup.py test -m <modulename>
    """
    return unittest.defaultTestLoader.loadTestsFromName(__name__)
