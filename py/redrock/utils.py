"""
Redrock utility functions.
"""

from __future__ import absolute_import, division, print_function

import sys
import os
import time

import numpy as np

from . import constants

import logging
logger = logging.getLogger("redrock.utils")

#- From https://github.com/desihub/desispec io.util.native_endian
def native_endian(data):
    """Convert numpy array data to native endianness if needed.

    Returns new array if endianness is swapped, otherwise returns input data

    By default, FITS data from astropy.io.fits.getdata() are not Intel
    native endianness and scipy 0.14 sparse matrices have a bug with
    non-native endian data.

    Args:
        data (array): input array

    Returns:
        array: original array if input in native endianness, otherwise a copy
            with the bytes swapped.

    """
    if data.dtype.isnative:
        return data
    else:
        return data.byteswap().newbyteorder()


def encode_column(c):
    """Returns a bytes column encoded into a string column.

    Args:
        c (Table column): a column of a Table.

    Returns:
        array: an array of strings.

    """
    return c.astype((str, c.dtype.itemsize))


#- Adapted from http://stackoverflow.com/a/21659588; unix only
def getch():
    """Return a single character from stdin.
    """
    import tty, termios
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


def elapsed(timer, prefix, comm=None):
    """Get and print the elapsed time.

    If timer is None, compute the start time and return.  Otherwise, find the
    elapsed time and print a message before returning the new start time.

    Args:
        timer (float): time in seconds for some arbitrary epoch.  If "None",
            get the current time and return.
        prefix (str): string to print before the elapsed time.
        comm (mpi4py.MPI.Comm): optional communicator.

    Returns:
        float: the new start time in seconds.

    """
    if comm is not None:
        comm.barrier()

    cur = time.time()
    if timer is not None:
        elapsed = cur - timer
        if ((comm is None) or (comm.rank == 0)):
            logger.info("{}: {:0.1f} seconds".format(prefix, elapsed))
            sys.stdout.flush()

    return cur


def nersc_login_node():
    """Returns True if we are on a NERSC login node, else False.
    """
    if ("NERSC_HOST" in os.environ) and ("SLURM_JOB_NAME" not in os.environ):
        return True
    else:
        return False


def get_mp(requested):
    """Returns a reasonable number of multiprocessing processes.

    This checks whether the requested value makes sense, and also whether we
    are running on a NERSC login node (and hence would get in trouble trying
    to use all cores).

    Args:
        requested (int): the requested number of processes.

    Returns:
        int: the number of processes to use.

    """
    import multiprocessing as mp

    mpmax = mp.cpu_count()
    mpbest = mpmax // 2
    if mpbest == 0:
        mpbest = 1

    procs = None
    if requested == 0:
        procs = mpbest
    elif requested > mpmax:
        print("Requested number of processes ({}) is too large, "
            "reducing to {}".format(requested, mpmax))
        sys.stdout.flush()
        procs = mpmax
    else:
        procs = requested

    # On NERSC login nodes, avoid hogging the whole node (and getting an
    # unhappy phone call).
    login_max = 4
    if nersc_login_node():
        if procs > login_max:
            logger.info("Running on a NERSC login node- reducing number of processes"
                " to {}".format(login_max))
            sys.stdout.flush()
            procs = login_max

    return procs


def mp_array(original):
    """Allocate a raw shared memory buffer and wrap it in an ndarray.

    This allocates a multiprocessing.RawArray and wraps the buffer
    with an ndarray.

    Args:
        typcode (str): the type code of the array.
        size_or_init: passed to the RawArray constructor.

    Returns;
        ndarray: the wrapped data.

    """
    import multiprocessing as mp

    typecode = original.dtype.char
    shape = original.shape

    raw = mp.RawArray(typecode, original.ravel())
    nd = np.array(raw, dtype=typecode, copy=False).view()
    nd.shape = shape
    return nd

def distribute_work(nproc, ids, weights=None, capacities=None):
    """Helper function to distribute work among processes with varying capacities.

    Args:
        nproc (int): the number of processes.
        ids (list): list of work unit IDs
        weights (dict): dictionary of weights for each ID. If None,
            use equal weighting.
        capacities (list): list of process capacities. If None,
            use equal capacity per process. A process with higher capacity
            can handle more work.

    Returns:
        list: A list (one element for each process) with each element
            being a list of the IDs assigned to that process.

    """
    # Sort ids by weights (descending)
    if weights is None:
        weights = { x : 1 for x in ids }
    sids = list(sorted(ids, key=lambda x: weights[x], reverse=True))

    # If capacities are not provided, assume they are equal
    if capacities is None:
        capacities = [1] * nproc

    # Initialize distributed list of ids
    dist = [list() for _ in range(nproc)]

    # Initialize process list. Processes are modeled using dictionary
    # with fields for a unique id, capacity, and load (total weight of work).
    processes = [dict(id=i, capacity=c, load=0) for i, c in enumerate(capacities)]

    for id in sids:
        w = weights[id]
        # Identify process to receive task. Smallest normalized load, break ties with capacity, followed by id.
        minload = min(processes, key=lambda p: ((p['load'] + w)/p['capacity'], 1/p['capacity'], p['id']))
        i = processes.index(minload)
        # Assign work unit to process
        minload['load'] += weights[id]
        dist[i].append(id)

    return dist


def transmission_Lyman(zObj,lObs):
    """Calculate the transmitted flux fraction from the Lyman series
    This returns the transmitted flux fraction:
    1 -> everything is transmitted (medium is transparent)
    0 -> nothing is transmitted (medium is opaque)
    Args:
        zObj (float): Redshift of object
        lObs (array of float): wavelength grid
    Returns:
        array of float: transmitted flux fraction
    """

    lRF = lObs/(1.+zObj)
    T   = np.ones(lObs.size)

    Lyman_series = constants.Lyman_series
    for l in list(Lyman_series.keys()):
        w      = lRF<Lyman_series[l]['line']
        zpix   = lObs[w]/Lyman_series[l]['line']-1.
        tauEff = Lyman_series[l]['A']*(1.+zpix)**Lyman_series[l]['B']
        T[w]  *= np.exp(-tauEff)

    return T
