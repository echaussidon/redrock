"""
redrock.zscan
=============

Algorithms for scanning redshifts.
"""

from __future__ import division, print_function

import sys
import traceback
import numpy as np

try:
    import cupy as cp
    import cupyx.scipy
    import cupyx
    cupy_available = cp.is_available()
except ImportError:
    cupy_available = False

from .utils import elapsed

from .targets import distribute_targets

if (cupy_available):
    block_size = 512 #Default block size, should work on all modern nVidia GPUs

    # cuda_source contains raw CUDA kernels to be loaded as CUPY module

    ###!!! NOTE - calc_M_and_y_atomic and calc_z_prod will be removed in
    ###    the final product

    cuda_source = r'''
        extern "C" {
            __global__ void batch_dot_product_sparse(const double* Rcsr_values, const int* Rcsr_cols, const int* Rcsr_indptr, const double* tdata, double* tb, int nrows, int ncols, int nbasis, int nt) {
                // This kernel performs a batch dot product of a sparse matrix Rcsr
                // with a set of redshifted templates
                //** Args:
                //       Rcsr_values, Rcsr_cols, Rcsr_indptr = individualarrays from sparse matrix
                //           (Rcsr = sparse array, ncols x nrows)
                //       tdata = redshifted templates, nt x ncols x nbasis
                //       tb = output array = nt x ncols x nbasis
                //       nrows, ncols, nbasis, nt = array dimensions

                const int i = blockDim.x*blockIdx.x + threadIdx.x; //thread index i - corresponds to the output array index
                if (i >= ncols*nbasis*nt) return;
                double x = 0; //define a local var to accumulate
                //ibatch, icol, it = index in 3d representation of output tb array
                //icol also == row in Rcsr input
                int ibatch = i % nbasis;
                int icol = (i % (nbasis*ncols)) / nbasis;
                int it = i / (nbasis*ncols);
                int t_start = it*nbasis*ncols; //first index in tdata for this thread
                int row = icol;

                int col;
                //loop over all nonzero entries in sparse matrix and compute dot product
                for (int j = Rcsr_indptr[row]; j < Rcsr_indptr[row+1]; j++) {
                    col = Rcsr_cols[j];
                    x += Rcsr_values[j] * tdata[t_start+nbasis*col+ibatch];
                }
                tb[i] = x;
                return;
            }

            __global__ void calc_M_and_y_atomic(const double* all_Tb, const double* weights, const double* wflux, double* M, double* y, int nrows, int nbasis, int nt, int nparallel) {
                // This kernel computes the dot products resulting in the M and y arrays in parallel
                // The y array is small compared to the M array so rather than launching a separate kernel,
                // a small number of threads will be diverted to compute y in parallel since the Tb array
                // is used to compute both.
                // It will use nparallel threads to compute the dot product for each output element in M and y.
                // Each thread will handle nrows/nparallel products and sums into an intermediate local variable
                // and then an atomicAdd will be used to add this intermediate sum to the output array.

                // This replicates the python commands:
                //     M = Tb.T.dot(np.multiply(weights[:,None], Tb))
                //     y = Tb.T.dot(wflux)
                //** Args:
                //       all_Tb = the Tb array, the stacked output from all 3 filters from
                //           batch_dot_product_sparse, for all redshift templates (nt x nrows x nbasis)
                //       weights = the weights array for this target (1d, size = nrows)
                //       wflux = the wflux array for this target (1d, size = nrows)
                //       M = the output M array (nt x nbasis x nbasis)
                //       y = the output y array (nt x nbasis)
                //       nrows, nbasis, nt = array dimensions
                //       nparallel = number of parallel threads to used for each output

                const int i = blockDim.x*blockIdx.x + threadIdx.x; //thread index i
                if (i >= nbasis*nbasis*nt*nparallel+nbasis*nt*nparallel) return;

                if (i < nbasis*nbasis*nt*nparallel) {
                    //These threads compute M
                    int m_idx = i / nparallel; //index in output M array
                    int t = m_idx / (nbasis*nbasis); //target number
                    int allTb_row = (m_idx % (nbasis*nbasis)) % nbasis; //row in all_Tb array
                    int wTb_row = (m_idx % (nbasis*nbasis)) / nbasis; // row in (weights*Tb)

                    int stride = nrows/nparallel; //stride to divide up nparallel threads

                    int start = (threadIdx.x % nparallel)*stride; //start index in nrows dim
                    int end = ((threadIdx.x % nparallel)+1)*stride; //end index in nrows dim
                    if (threadIdx.x % nparallel == (nparallel-1)) end = nrows;
                    int allTb_idx = t*nrows*nbasis + allTb_row; // 1-d index for first element to be processed by this thread
                    int wTb_idx = t*nrows*nbasis + wTb_row; // 1-d index for first element to be processed by this thread

                    double x = 0; //define local var to accumulate

                    //perform intermediate sum dot product for this thread
                    for (int j = start; j < end; j++) {
                        //stride by nbasis
                        x += all_Tb[allTb_idx+j*nbasis] * (all_Tb[wTb_idx+j*nbasis] * weights[j]);
                    }
                    //use atomic add to avoid collisions between threads
                    atomicAdd(&M[m_idx], x);
                } else {
                    //These threads compute y
                    int i2 = (i-nbasis*nbasis*nt*nparallel); //index among y-threads
                    int y_idx = i2 / nparallel; //index in output y array
                    int t = y_idx / nbasis; //target number
                    int allTb_row = y_idx % nbasis; //row in all_Tb array

                    int stride = nrows/nparallel; //stride to divide up nparallel threads

                    int start = (threadIdx.x % nparallel)*stride; //start index in nrows dim
                    int end = ((threadIdx.x % nparallel)+1)*stride; //end index in nrows dim
                    if (threadIdx.x % nparallel == (nparallel-1)) end = nrows;
                    int allTb_idx = t*nrows*nbasis + allTb_row; // 1-d index for first element to be processed by this thread

                    double x = 0; //define local var to accumulate

                    //perform intermediate sum dot product for this thread
                    for (int j = start; j < end; j++) {
                        //stride by nbasis
                        x += all_Tb[allTb_idx+j*nbasis] * wflux[j];
                    }
                    //use atomic add to avoid collisions between threads
                    atomicAdd(&y[y_idx], x);
                }
            }

            __global__ void batch_dot_product_3d3d(const double* a, const double* b, double* M, int nrows, int nbasis, int nt, int nparallel, int transpose_a) {
                // This kernel computes a batch dot product of two 3-d arrays,
                // a and b such that for every i
                //     M[i,:,:] = a[i,:,:].dot(b[i,:,:])
                // This replicates the CUPY code M = a @ b but more efficiently
                // It will use nparallel threads to compute the dot product
                // for each output element in M.  Each thread will handle
                //  nrows/nparallel products and sums into an intermediate
                // local variable // and then an atomicAdd will be used to add
                // this intermediate sum to the output array.

                // This replicates the python command:
                //     M = Tb.T.dot(np.multiply(weights[:,None], Tb))
                // for all M where a = Tb.T and b = weights[:,None]*Tb
                //
                //** Args:
                //       a = a 3-d array
                //       b = a 3-d array
                //       weights = the weights array for this target (1d, size = nrows)
                //       wflux = the wflux array for this target (1d, size = nrows)
                //       M = the output M array (nt x nbasis x nbasis)
                //       y = the output y array (nt x nbasis)
                //       nrows, nbasis, nt = array dimensions
                //       nparallel = number of parallel threads to used for each output

                const int i = blockDim.x*blockIdx.x + threadIdx.x; //thread index i
                if (i >= nbasis*nbasis*nt*nparallel) return;

                int m_idx = i / nparallel; //index in output M array
                int t = m_idx / (nbasis*nbasis); //target number
                int a_row = (m_idx % (nbasis*nbasis)) % nbasis; //row in a array
                int b_row = (m_idx % (nbasis*nbasis)) / nbasis; //row in b array

                int stride = nrows/nparallel; //stride to divide up nparallel threads

                int start = (threadIdx.x % nparallel)*stride; //start index in nrows dim
                int end = ((threadIdx.x % nparallel)+1)*stride; //end index in nrows dim
                if (threadIdx.x % nparallel == (nparallel-1)) end = nrows;
                int a_idx = t*nrows*nbasis + a_row*nrows; // 1-d index for first element to be processed by this thread
                if (transpose_a) a_idx = t*nrows*nbasis + a_row;
                int b_idx = t*nrows*nbasis + b_row; // 1-d index for first element to be processed by this thread
                double x = 0; //define local var to accumulate

                //perform intermediate sum dot product for this thread
                if (transpose_a) {
                    for (int j = start; j < end; j++) {
                        //stride by nbasis
                        x += a[a_idx+j*nbasis] * b[b_idx+j*nbasis];
                    }
                } else {
                    for (int j = start; j < end; j++) {
                        //stride by nbasis
                        x += a[a_idx+j] * b[b_idx+j*nbasis];
                    }
                }
                //use atomic add to avoid collisions between threads
                atomicAdd(&M[m_idx], x);
            }

            __global__ void batch_dot_product_3d2d(const double* tb, const double* zc, double* model, int nrows, int nbasis, int nt) {
                // This kernel computes a batch dot product of Tb (a 3-d array)
                // and zc (a 2-d array), the result of the matrix solution of
                // M and y, for all templates (nt) in parallel.  It results in
                // the 2-d model array.  Each thread computes an element in the
                // output model array.  It replaces the python code:
                //     model = Tb.dot(cupy.array(zc))
                //** Args:
                //       tb = the Tb array, the stacked output from all 3 filters from
                //           batch_dot_product_sparse, for all redshift templates (nt x nrows x nbasis)
                //       zc = the zc array, the output of
                //           zc = cp.linalg.solve(all_M, all_y)
                //           shape = (nt x nbasis)
                //       model = the output of the dot product, (nt x nrows)
                const int i = blockDim.x*blockIdx.x + threadIdx.x; //thread index i
                if (i >= nrows*nt) return;
                int it = i / nrows; //target num
                int row = i % nrows; //row num
                int i_tb = it * nrows * nbasis + row * nbasis; //start index in Tb array
                int i_zc = it * nbasis; //start index in zc array
                double x = 0; //use local var to accumulate
                //compute dot product
                for (int j = 0; j < nbasis; j++) {
                    x += tb[i_tb+j] * zc[i_zc+j];
                }
                //copy to output
                model[i] = x;
            }

            __global__ void calc_z_prod(const double* flux, const double* model, const double* weights, double* z_product, int nrows, int nt) {
                // This kernel computes the dot product of (flux-model)^2 and weights
                // that results in the final zchi2 for all templates and one target.
                // It replaces the python code:
                //     zchi2[i,j] = cupy.dot((flux-model)**2, weights)
                //** Args:
                //       flux = the flux array for this target (1d, size = nrows)
                //       model = the output of batch_dot_product_3d2d (nt x nrows)
                //       weights = the weights array for this target (1d, size = nrows)
                //       z_product = the output of the dot product (nt x nrows)
                const int i = blockDim.x*blockIdx.x + threadIdx.x; //thread index i
                if (i >= nrows*nt) return;
                int it = i / nrows; //target num
                int row = i % nrows; //row num
                int i_model = it * nrows + row; //index in model array
                double x = flux[row]-model[i_model];
                z_product[i] = x*x*weights[row];
            }

        }
    '''

###!!! NOTE - this is used by original GPU algorithm and will be removed
def _zchi2_batch(Tb, weights, flux, wflux, zcoeff):
    """Calculate a batch of chi2.

    For many redshifts and a set of spectral data, compute the chi2 for template
    data that is already on the correct grid.
    """

    M = Tb.swapaxes(-2, -1) @ (weights[None, :, None] * Tb)
    y = (Tb.swapaxes(-2, -1) @ wflux)
    # TODO: use cholesky solve here?
    zcoeff[:] = np.linalg.solve(M, y)
    model = np.squeeze((Tb @ zcoeff[:, :, None]))
    zchi2 = ((flux - model)**2 @ weights)
    return zchi2

###!!! NOTE - This is used by original CPU algorithm and is replaced in new
###    v2 and v3 versions so will be removed
def _zchi2_one(Tb, weights, flux, wflux, zcoeff):
    """Calculate a single chi2.

    For one redshift and a set of spectral data, compute the chi2 for template
    data that is already on the correct grid.
    """

    M = Tb.T.dot(np.multiply(weights[:,None], Tb))
    y = Tb.T.dot(wflux)

    try:
        zcoeff[:] = np.linalg.solve(M, y)
    except np.linalg.LinAlgError:
        return 9e99

    model = Tb.dot(zcoeff)

    zchi2 = np.dot( (flux - model)**2, weights )

    return zchi2

def spectral_data(spectra):
    """Compute concatenated spectral data products.

    This helper function builds full length array quantities needed for the
    chi2 fit.

    Args:
        spectra (list): list of Spectrum objects.

    Returns:
        tuple: (weights, flux, wflux) concatenated values used for single
            redshift chi^2 fits.

    """
    weights = np.concatenate([ s.ivar for s in spectra ])
    flux = np.concatenate([ s.flux for s in spectra ])
    wflux = weights * flux
    return (weights, flux, wflux)

###!!! NOTE - This is used by original CPU algorithm and is replaced in new
###    v2 and v3 versions so will be removed
def calc_zchi2_one(spectra, weights, flux, wflux, tdata):
    """Calculate a single chi2.

    For one redshift and a set of spectra, compute the chi2 for template
    data that is already on the correct grid.

    Args:
        spectra (list): list of Spectrum objects.
        weights (array): concatenated spectral weights (ivar).
        flux (array): concatenated flux values.
        wflux (array): concatenated weighted flux values.
        tdata (dict): dictionary of interpolated template values for each
            wavehash.

    Returns:
        tuple: chi^2 and coefficients.

    """
    Tb = list()
    nbasis = None
    for s in spectra:
        key = s.wavehash
        if nbasis is None:
            nbasis = tdata[key].shape[1]
            #print("using ",nbasis," basis vectors", flush=True)
        Tb.append(s.Rcsr.dot(tdata[key]))
    Tb = np.vstack(Tb)
    zcoeff = np.zeros(nbasis, dtype=np.float64)
    zchi2 = _zchi2_one(Tb, weights, flux, wflux, zcoeff)

    return zchi2, zcoeff

###!!! NOTE - This is the original main algorithm.  Right now it will
###    propagate to any of the other algorithms with calls documented below.
###    In final version, it will be replaced by the algorithm we select
###    (v2 or v3).  For thd draft PR, the default is set to run v3.
def calc_zchi2(target_ids, target_data, dtemplate, progress=None, use_gpu=False):
    """Calculate chi2 vs. redshift for a given PCA template.

    Args:
        target_ids (list): targets IDs.
        target_data (list): list of Target objects.
        dtemplate (DistTemplate): distributed template data
        progress (multiprocessing.Queue): optional queue for tracking
            progress, only used if MPI is disabled.
        use_gpu (bool): (optional) use gpu for calc_zchi2

    Returns:
        tuple: (zchi2, zcoeff, zchi2penalty) with:
            - zchi2[ntargets, nz]: array with one element per target per
                redshift
            - zcoeff[ntargets, nz, ncoeff]: array of best fit template
                coefficients for each target at each redshift
            - zchi2penalty[ntargets, nz]: array of penalty priors per target
                and redshift, e.g. to penalize unphysical fits

    """
    ###!!! NOTE - uncomment the below line to run v2 algorithm
    #return calc_zchi2_v2(target_ids, target_data, dtemplate, progress, use_gpu)
    ###!!! NOTE - uncomment the below line to run v3 algorithm
    return calc_zchi2_v3(target_ids, target_data, dtemplate, progress, use_gpu)
    if use_gpu:
        ###!!! NOTE - uncomment the below line to run original GPU algorithm
        #return calc_zchi2_gpu(target_ids, target_data, dtemplate, progress)
        ###!!! NOTE - uncomment the below line to run v1 GPU algorithm
        return calc_zchi2_gpu_new(target_ids, target_data, dtemplate, progress)
    nz = len(dtemplate.local.redshifts)
    ntargets = len(target_ids)
    nbasis = dtemplate.template.nbasis

    zchi2 = np.zeros( (ntargets, nz) )
    zchi2penalty = np.zeros( (ntargets, nz) )
    zcoeff = np.zeros( (ntargets, nz, nbasis) )

    # Redshifts near [OII]; used only for galaxy templates
    if dtemplate.template.template_type == 'GALAXY':
        isOII = (3724 <= dtemplate.template.wave) & \
            (dtemplate.template.wave <= 3733)
        OIItemplate = dtemplate.template.flux[:,isOII].T

    for j in range(ntargets):
        (weights, flux, wflux) = spectral_data(target_data[j].spectra)

        # Loop over redshifts, solving for template fit
        # coefficients.  We use the pre-interpolated templates for each
        # unique wavelength range.
        for i, _ in enumerate(dtemplate.local.redshifts):
            zchi2[j,i], zcoeff[j,i] = calc_zchi2_one(target_data[j].spectra,
                weights, flux, wflux, dtemplate.local.data[i])

            #- Penalize chi2 for negative [OII] flux; ad-hoc
            if dtemplate.template.template_type == 'GALAXY':
                OIIflux = np.sum( OIItemplate.dot(zcoeff[j,i]) )
                if OIIflux < 0:
                    zchi2penalty[j,i] = -OIIflux

        if dtemplate.comm is None:
            progress.put(1)

    return zchi2, zcoeff, zchi2penalty

###!!! NOTE - used in v2 and v3 algorithms (v3 only for GPU)
def batch_dot_product_sparse(spectra, tdata, nz, use_gpu):
    """Calculate a batch dot product of the 3 sparse matrices in spectra
    with every template in tdata.  Sparse matrix libraries are used
    to perform the dot products.

    Args:
        spectra (list): list of Spectrum objects.
        tdata (dict): dictionary of interpolated template values for each
            wavehash.
        nz (int): number of templates
        use_gpu (bool): use GPU or not

    Returns:
        Tbs (list): dot products of these 3 spectra with all templates

    """

    if (use_gpu):
        #Use GPU to do dot products in batch
        return batch_dot_product_sparse_gpu(spectra, tdata)

    #Need to find shape of output array of batch dot product
    nrows = 0
    nbasis = None
    for key in tdata:
        nrows += tdata[key].shape[1]
        if (nbasis is None):
            nbasis = tdata[key].shape[2]

    #Create empty array rather than stacking a list - faster
    Tbs = np.empty((nz, nrows, nbasis))
    #Loop over all templates
    for i in range(nz):
        irow = 0
        for s in spectra:
            key = s.wavehash
            curr_tb = s.Rcsr.dot(tdata[key][i,:,:])
            #Copy this dot product result into the Tbs array
            Tbs[i, irow:irow+curr_tb.shape[0],:] = curr_tb
            irow += curr_tb.shape[0]
    return Tbs

###!!! NOTE - used in v2 and v3 algorithms
def batch_dot_product_sparse_gpu(spectra, tdata):
    """GPU implementation.
    Calculate a batch dot product of the 3 sparse matrices in spectra
    with every template in tdata.  A CUDA kernel replicates the functionality
    of the scipy sparse matrix dot product but done for every template
    in parallel so that the kernel is only called once per spectrum.

    Args:
        spectra (list): list of Spectrum objects.
        tdata (dict): dictionary of interpolated template values for each
            wavehash.

    Returns:
        Tbs (cp.array): dot products of these 3 spectra with all templates

    """

    # Load CUDA kernel
    cp_module = cp.RawModule(code=cuda_source)
    batch_dot_product_sparse_kernel = cp_module.get_function('batch_dot_product_sparse')
    Tbs = list()

    for s in spectra:
        key = s.wavehash
        #Array dimensions needed by CUDA kernel
        nrows = cp.int32(s.Rcsr.shape[1])
        ncols = cp.int32(s.Rcsr.shape[0])
        nbasis = cp.int32(tdata[key].shape[2])
        nt = cp.int32(tdata[key].shape[0])

        #Use actual numpy arrays that represent sparse array - .data, .indices, and .indptr
        #Use batch_dot_product_sparse kernel to perform dot product in parallel for all templates
        #for this (target, spectrum) combination.
        #Allocate CUPY arrays and calculate number of blocks to use.
        n = tdata[key].size
        blocks = (n+block_size-1)//block_size
        Rcsr_values = cp.array(s.Rcsr.data, cp.float64)
        Rcsr_cols = cp.array(s.Rcsr.indices, cp.int32)
        Rcsr_indptr = cp.array(s.Rcsr.indptr, cp.int32)
        curr_tb = cp.empty((nt, ncols, nbasis))
        #Launch kernel and syncrhronize
        batch_dot_product_sparse_kernel((blocks,), (block_size,), (Rcsr_values, Rcsr_cols, Rcsr_indptr, tdata[key], curr_tb, nrows, ncols, nbasis, nt))

        #Commented out synchronize - needed for timing kernels but we still
        #get execution of this kernel before data is needed for next kernel
        #so output is the same and slightly faster without synchronize
        #cp.cuda.Stream.null.synchronize()
        #Append to list
        Tbs.append(curr_tb)
    #Use CUPY.hstack to combine into one nt x ncols x nbasis array
    Tbs = cp.hstack(Tbs)
    #cp.cuda.Stream.null.synchronize()
    return Tbs

###!!! NOTE - used in v3 algorithm instead of batch_dot_product_sparse for CPU
def dot_product_sparse_one(spectra, tdata):
    """Calculate a dot product of the 3 sparse matrices in spectra
    with ONE template in tdata.  Sparse matrix libraries are used
    to perform the dot products.

    Args:
        spectra (list): list of Spectrum objects.
        tdata (dict): dictionary of interpolated template values for each
            wavehash for ONE template.

    Returns:
        Tb (array): dot products of these 3 spectra with ONE templates

    """

    Tb = list()
    for s in spectra:
        key = s.wavehash
        Tb.append(s.Rcsr.dot(tdata[key]))
    Tb = np.vstack(Tb)
    return Tb

###!!! NOTE - This will be removed in the final version but is included
###    here for the ability to run timing tests - this is the fastest version
###    of calculating the M and y arrays via a custom GPU kernel.
def calc_M_y_batch(Tbs, weights, wflux, nz, nbasis):
    """Use calc_M_y_atomic kernel to compute M and y arrays
    nparallel - number of parallel threads for each output array element
    For larger input Tbs arrays - e.g., GALAXY, QSO, 4 parallel threads
    is faster because we don't want to create too many total threads
    But for smaller Tb arrays - STARS - we can use more parallel threads
    to maximize parallelism - this can be dynamically tuned but in test
    data, 4 and 64 were optimal.  Needs to be power of 2.

    Args:
        Tbs (cp.array): the stacked output from all 3 filters from
            batch_dot_product_sparse, for all redshift templates
            (nz x nrows x nbasis)
        weights (cp.array): concatenated spectral weights (ivar).
        wflux (cp.array): concatenated weighted flux values
        nz (int): number of templates
        nbasis (int): nbasis

    Returns:
        all_M (cp.array): 3-d array composed of n_templates 2-d arrays,
            intermediate data product in chi square computation
        all_y (cp.array): 2-d array composed of n_templates 1-d arrays,
            intermediate data product in chi square computation
    """

    # Load CUDA kernel
    cp_module = cp.RawModule(code=cuda_source)
    calc_M_y = cp_module.get_function('calc_M_and_y_atomic')

    if (nz > 512):
        nparallel = cp.int32(4)
    else:
        nparallel = cp.int32(64)
    #Create CUPY arrays and calculate number of blocks
    nrows = cp.int32(Tbs.shape[1])
    n = nz*nbasis*nbasis*nparallel + nz*nbasis*nparallel
    blocks = (n+block_size-1)//block_size

    all_M = cp.zeros((nz, nbasis, nbasis))
    all_y = cp.zeros((nz, nbasis))

    #Launch kernel and syncrhonize
    calc_M_y((blocks,), (block_size,), (Tbs, weights, wflux, all_M, all_y, nrows, nbasis, nz, nparallel))
    #Commented out synchronize - needed for timing kernels but we still
    #get execution of this kernel before data is needed for next kernel
    #so output is the same and slightly faster without synchronize
    #cp.cuda.Stream.null.synchronize()
    return (all_M, all_y)

###!!! NOTE - used in v2 and v3 algorithms (v3 only for GPU)
def linalg_solve_batch(all_M, all_y, use_gpu):
    """Use the numpy linalg.solve (or its cupy equivalent) to solve
    the linear matrix equation M x = y for x. In cupy it is done
    in batch for all M and all Y - in numpy each template is looped
    over.

    Args:
        all_M (array): 3-d array composed of n_templates 2-d arrays,
            intermediate data product in chi square computation
        all_y (array): 2-d array composed of n_templates 1-d arrays,
            intermediate data product in chi square computation
        use_gpu (bool): use GPU or not

    Returns:
        zcoeff (array): x, the solution to M x = y from linalg.solve
            a 2-d array with the same shape as all_y.
        iserr (array): 1-d array indicating if there was a
            np.linalg.LinAlgError exception thrown for this template

    """

    nz = all_M.shape[0]
    nbasis = all_M.shape[1]
    #bool array to track elements with LinAlgError from np.linalg.solve
    iserr = np.zeros(nz, dtype=np.bool)
    if (use_gpu):
        #Normal case on Perlmutter - solve for all templates at once
        zcoeff = cp.linalg.solve(all_M, all_y)
        return (zcoeff, iserr)
    else:
        #Create output array
        zcoeff = np.zeros((nz, nbasis), dtype=np.float64)
        #Loop over each template on CPU
        for i in range(nz):
            M = all_M[i,:,:]
            y = all_y[i,:]
            try:
                zcoeff[i,:] = np.array(np.linalg.solve(M, y))
            except np.linalg.LinAlgError:
                iserr[i] = True
                continue
    return (zcoeff, iserr)


###!!! NOTE - used in v2 and v3 algorithms (v3 only for GPU)
def calc_batch_dot_product_3d2d(Tbs, zc, use_gpu):
    """Calculate a batch dot product of the 3d array Tbs with the 2d
    array zc.  The 3-d array shape is A x B x C and the 2-d array
    shape is A x C.  The resulting output 2-d array shape is A x B.
    E.g., for all A a dot product of a 2d array of shape B x C
    is performed with a 1-d array of shape C.
    These are non-sparse numpy arrays.

    Args:
        Tbs (array): the stacked output from all 3 filters from
            batch_dot_product_sparse, for all redshift templates
            (nz x nrows x nbasis)
        zc (array): zcoeffs, the 2-d array output of
            zc = linalg.solve(all_M, all_y)
            (nz x nbasis)
        use_gpu (bool): use GPU or not

    Returns:
        model (array): the output of the dot product, (nz x nrows)

    """

    if (use_gpu):
        return calc_batch_dot_product_3d2d_gpu(Tbs, zc)

    #Get array dims to reshape model array to 2d
    nz = zc.shape[0]
    nrows = Tbs[0].shape[0]
    model = (Tbs@zc[:, :, None]).reshape((nz, nrows))
    return model


###!!! NOTE - used in v2 and v3 algorithms
def calc_batch_dot_product_3d2d_gpu(Tbs, zc):
    """GPU implementation.
    Calculate a batch dot product of the 3d array Tbs with the 2d
    array zc.  The 3-d array shape is A x B x C and the 2-d array
    shape is A x C.  The resulting output 2-d array shape is A x B.
    E.g., for all A a dot product of a 2d array of shape B x C
    is performed with a 1-d array of shape C.
    These are non-sparse numpy arrays.

    Args:
        Tbs (array): the stacked output from all 3 filters from
            batch_dot_product_sparse, for all redshift templates
            (nz x nrows x nbasis)
        zc (array): zcoeffs, the 2-d array output of
            zc = linalg.solve(all_M, all_y)
            (nz x nbasis)

    Returns:
        model (array): the output of the dot product, (nz x nrows)

    """

    #Use batch_dot_product_3d2d kernel to compute model array
    # Load CUDA kernel
    cp_module = cp.RawModule(code=cuda_source)
    batch_dot_product_3d2d_kernel = cp_module.get_function('batch_dot_product_3d2d')

    #Array dims needed by CUDA:
    nz = cp.int32(zc.shape[0])
    nrows = Tbs[0].shape[0]
    n = nrows * nz
    nbasis = cp.int32(zc.shape[1])

    #Allocate CUPY array and calc blocks to be used
    blocks = (n+block_size-1)//block_size
    model = cp.empty((nz, nrows), cp.float64)
    #Launch kernel and synchronize
    batch_dot_product_3d2d_kernel((blocks,), (block_size,), (Tbs, zc, model, nrows, nbasis, nz))
    #cp.cuda.Stream.null.synchronize()
    return model

###!!! NOTE - used in v2 and v3 algorithms as an alternative to straight CuPy
###    computation of M and y or the calc_M_y_batch method as a middle ground
###    between maximum speed and maximum maintainability.
###    This only offloads the computationally expensive dot product itself
###    (and optionally the transpose) because the CuPy @ matrix multiplication
###    seems to have a bug on Volta architecure GPUs.
###    This is the equivalent of M = a @ b
###    (Or if transpose_a is true, M = a.swapaxes(-2, -1) @ b)
def calc_batch_dot_product_3d3d_gpu(a, b, transpose_a=False):
    """GPU implementation.
    Calculate a batch dot product of the 3d array a with the 3d
    array b.  The 3-d array shape is A x B x C and the 2-d array
    shape is A x C.  The resulting output 2-d array shape is A x B.
    E.g., for all A a dot product of a 2d array of shape B x C
    is performed with a 1-d array of shape C.
    These are non-sparse numpy arrays.

    Args:
        a (array): a 3-d array (nz x ncols x nrows)
            In practice, the Tb array, the stacked output from all 3 filters
            from batch_dot_product_sparse, for all redshift templates
            (nz x nrows x nbasis) which should have its transpose
            performed yielding shape (nz x nbasis x nrows).
        b (array): another 3-d array (nz x nrows x ncols)
            In practice, the Tb array multiplied y weights, shape
            (nz x nrows x nbasis)
        transpose_a (bool): Whether or not to transpose the a array
            before performing the dot product

    Returns:
        M (array): the output of the dot product, (nz x ncols x ncols)
            such that M[i,:,:] = a[i,:,:].dot(b[i,:,:])

    """

    #Use batch_dot_product_3d3d kernel to compute model array
    # Load CUDA kernel
    cp_module = cp.RawModule(code=cuda_source)
    batch_dot_product_3d3d_kernel = cp_module.get_function('batch_dot_product_3d3d')

    #Array dims needed by CUDA:
    nz = cp.int32(a.shape[0])
    if (transpose_a):
        nrows = cp.int32(a.shape[1])
        ncols = cp.int32(a.shape[2])
    else:
        nrows = cp.int32(a.shape[2])
        ncols = cp.int32(a.shape[1])
    transpose_a = cp.int32(transpose_a)

    if (nz > 512):
        nparallel = cp.int32(4)
    else:
        nparallel = cp.int32(64)
    #Create CUPY arrays and calculate number of blocks
    n = nz*ncols*ncols*nparallel
    blocks = (n+block_size-1)//block_size
    all_M = cp.zeros((nz, ncols, ncols))

    #Launch kernel and synchronize
    batch_dot_product_3d3d_kernel((blocks,), (block_size,), (a, b, all_M, nrows, ncols, nz, nparallel, transpose_a))
    #cp.cuda.Stream.null.synchronize()
    return all_M


###!!! NOTE - this is called in the v2 algorithm
###    In this version, everything is done in batch on both GPU and CPU
###    E.g. we calculate M and y for all templates and store them in
###    3d and 2d arrays respetively, then do a linalg.solve for each one...
def calc_zchi2_batch_v2(Tbs, weights, flux, wflux, nz, nbasis, use_gpu):
    """Calculate a batch of chi2.
    For many redshifts and a set of spectral data, compute the chi2 for
    template data that is already on the correct grid.

    Args:
        Tbs (array): the stacked output from all 3 filters from
            batch_dot_product_sparse, for all redshift templates
            (nt x nrows x nbasis)
        weights (array): concatenated spectral weights (ivar).
        flux (array): concatenated flux values.
        wflux (array): concatenated weighted flux values.
        nz (int): number of templates
        nbasis (int): nbasis
        use_gpu (bool): use GPU or not

    Returns:
        zchi2 (array): array with one element per redshift for this target
        zcoeff (array): array of best fit template coefficients for
            this target at each redshift
    """

    zchi2 = np.zeros(nz)
    if (use_gpu):
        ###!!! NOTE - there are 3 different options for calculating the
        ###    M and y arrays -
        ###    A) Straight CUPY, which works well on perlmutter with a
        ###        runtime of 6.2s on 1 GPU and 2.0s on 4 GPUs, but is
        ###        unusably slow on Volta generation GPUs (16.8s for only
        ###        10 targets on a 1660 Super).
        ###    B) calc_M_y_batch, the custom CUDA kernel, which is the
        ###        fastest at 2.9s on 1 GPU and 0.7s on 4 GPUs (and 0.7s
        ###        for 10 targets on a 1660 Super) but is the most difficult
        ###        from a maintenance perspective
        ###    C) Use the calc_batch_dot_product_3d3d_gpu kernel to offload
        ###        only the matrix multiplication for M (and transpose of
        ###        Tbs) but use CUPY for everything else.  This strikes a
        ###        middle ground that is very maintainable but removes the
        ###        bottleneck of the CUPY Volta issue.  5.7s on 1 GPU and
        ###        1.8s on 4 GPUs on Perlmutter; 1.6s for 10 targets on
        ###        1660 Super.
        ###!!! NOTE - uncomment the 2 lines below to run (A)
        #all_M = Tbs.swapaxes(-2, -1) @ (weights[None, :, None] * Tbs)
        #all_y = (Tbs.swapaxes(-2, -1) @ wflux)
        ###!!! NOTE - uncomment the below line to run (B)
        #(all_M, all_y) = calc_M_y_batch(Tbs, weights, wflux, nz, nbasis)
        ###!!! NOTE - uncomment the 2 lines below to run (C)
        all_M = calc_batch_dot_product_3d3d_gpu(Tbs, (weights[None, :, None] * Tbs), transpose_a=True)
        all_y = (Tbs.swapaxes(-2, -1) @ wflux)
        ###!!! NOTE - uncomment the 2 lines below to run an alternative
        ###    version of (C) that does the transpose on the CPU - this seems
        ###    to needlessly waste time though
        #all_M = calc_batch_dot_product_3d3d_gpu(cp.ascontiguousarray(Tbs.swapaxes(-2, -1)), (weights[None, :, None] * Tbs))
        #all_y = (Tbs.swapaxes(-2, -1) @ wflux)
    else:
        all_M = np.zeros((nz, nbasis, nbasis))
        all_y = np.zeros((nz, nbasis))
        nrows = Tbs[0].shape[0]
        for i in range(nz):
            all_M[i,:,:] = Tbs[i].T.dot(np.multiply(weights[:,None], Tbs[i]))
            all_y[i,:] = Tbs[i].T.dot(wflux)

    (zcoeff, iserr) = linalg_solve_batch(all_M, all_y, use_gpu)
    model = calc_batch_dot_product_3d2d(Tbs, zcoeff, use_gpu)

    if (use_gpu):
        zchi2[:] = (((flux - model)*(flux-model)) @ weights).get()
        zcoeff = zcoeff.get()
    else:
        for i in range(nz):
            zchi2[i] = np.dot((flux-model[i,:])**2, weights)

    zchi2[iserr] = 9e99
    return (zchi2, zcoeff)


###!!! NOTE - this is the main method for the v2 algorithm
###    In this version, everything is done in batch on both GPU and CPU
###    E.g. we calculate M and y for all templates and store them in
###    3d and 2d arrays respetively, then do a linalg.solve for each one...
def calc_zchi2_v2(target_ids, target_data, dtemplate, progress=None, use_gpu=False):
    """Calculate chi2 vs. redshift for a given PCA template.

    New CPU/GPU algorithms 4/22/22

    Args:
        target_ids (list): targets IDs.
        target_data (list): list of Target objects.
        dtemplate (DistTemplate): distributed template data
        progress (multiprocessing.Queue): optional queue for tracking
            progress, only used if MPI is disabled.

    Returns:
        tuple: (zchi2, zcoeff, zchi2penalty) with:
            - zchi2[ntargets, nz]: array with one element per target per
                redshift
            - zcoeff[ntargets, nz, ncoeff]: array of best fit template
                coefficients for each target at each redshift
            - zchi2penalty[ntargets, nz]: array of penalty priors per target
                and redshift, e.g. to penalize unphysical fits

    """
    nz = len(dtemplate.local.redshifts)
    ntargets = len(target_ids)
    nbasis = dtemplate.template.nbasis

    zchi2 = np.zeros( (ntargets, nz) )
    zchi2penalty = np.zeros( (ntargets, nz) )
    zcoeff = np.zeros( (ntargets, nz, nbasis) )

    # Redshifts near [OII]; used only for galaxy templates
    if dtemplate.template.template_type == 'GALAXY':
        isOII = (3724 <= dtemplate.template.wave) & \
            (dtemplate.template.wave <= 3733)
        OIItemplate = dtemplate.template.flux[:,isOII].T

    tdata = dict()
    # Combine redshifted templates
    for key in dtemplate.local.data[0].keys():
        if (use_gpu):
            tdata[key] = cp.array([tdata[key] for tdata in dtemplate.local.data])
        else:
            tdata[key] = np.array([tdata[key] for tdata in dtemplate.local.data])

    for j in range(ntargets):
        (weights, flux, wflux) = spectral_data(target_data[j].spectra)
        if np.sum(weights) == 0:
            zchi2[j,:] = 9e99
            continue
        if (use_gpu):
            #Convert to cp.arrays
            weights = cp.array(weights)
            flux = cp.array(flux)
            wflux = cp.array(wflux)

        # Solving for template fit coefficients for all redshifts.
        # We use the pre-interpolated templates for each
        # unique wavelength range.

        # Use helper method batch_dot_product_sparse to create dot products
        # of all three spectra for this target with all templates
        Tbs = batch_dot_product_sparse(target_data[j].spectra, tdata, nz, use_gpu)
        (zchi2[j,:], zcoeff[j,:,:]) = calc_zchi2_batch_v2(Tbs, weights, flux, wflux, nz, nbasis, use_gpu)

        #Free data from GPU
        del Tbs

        #- Penalize chi2 for negative [OII] flux; ad-hoc
        if dtemplate.template.template_type == 'GALAXY':
            OIIflux = np.sum(zcoeff[j] @ OIItemplate.T, axis=1)
            zchi2penalty[j][OIIflux < 0] = -OIIflux[OIIflux < 0]

        if dtemplate.comm is None:
            progress.put(1)

    return zchi2, zcoeff, zchi2penalty

###!!! NOTE - this is called in the v3 algorithm
###    In this version, everything is done in batch on the GPU but the
###    templates are looped over on the CPU.  The operations performed
###    are very obviously analagous though and should be highly
###    maintainable.  The main difference is the extra loop on the CPU version
def calc_zchi2_batch_v3(spectra, tdata, weights, flux, wflux, nz, nbasis, use_gpu):
    """Calculate a batch of chi2.
    For many redshifts and a set of spectral data, compute the chi2 for
    template data that is already on the correct grid.

    Args:
        Tbs (array): the stacked output from all 3 filters from
            batch_dot_product_sparse, for all redshift templates
            (nt x nrows x nbasis)
        weights (array): concatenated spectral weights (ivar).
        flux (array): concatenated flux values.
        wflux (array): concatenated weighted flux values.
        nz (int): number of templates
        nbasis (int): nbasis
        use_gpu (bool): use GPU or not

    Returns:
        zchi2 (array): array with one element per redshift for this target
        zcoeff (array): array of best fit template coefficients for
            this target at each redshift
    """
    zchi2 = np.zeros(nz)
    if (use_gpu):
        #On the GPU, all operations are batch operations for all templates
        #in parallel.

        #1) batch_dot_product_sparse will compute dot products of all
        #spectra with all templates in batch and return a 3D array of
        #size (nz x ncols x nbasis).
        Tbs = batch_dot_product_sparse(spectra, tdata, nz, use_gpu)

        #2) On the GPU, M and y are computed for all templates at once
        #CUPY swapaxes is the equivalent of the transpose in CPU mode
        #and the @ matrix multiplication operator performs a dot
        #product for each template.

        ###!!! NOTE - there are 3 different options for calculating the
        ###    M and y arrays -
        ###    A) Straight CUPY, which works well on perlmutter with a
        ###        runtime of 6.2s on 1 GPU and 2.0s on 4 GPUs, but is
        ###        unusably slow on Volta generation GPUs (16.8s for only
        ###        10 targets on a 1660 Super).
        ###    B) calc_M_y_batch, the custom CUDA kernel, which is the
        ###        fastest at 2.9s on 1 GPU and 0.7s on 4 GPUs (and 0.7s
        ###        for 10 targets on a 1660 Super) but is the most difficult
        ###        from a maintenance perspective
        ###    C) Use the calc_batch_dot_product_3d3d_gpu kernel to offload
        ###        only the matrix multiplication for M (and transpose of
        ###        Tbs) but use CUPY for everything else.  This strikes a
        ###        middle ground that is very maintainable but removes the
        ###        bottleneck of the CUPY Volta issue.  5.7s on 1 GPU and
        ###        1.8s on 4 GPUs on Perlmutter; 1.6s for 10 targets on
        ###        1660 Super.
        ###!!! NOTE - uncomment the 2 lines below to run (A)
        #all_M = Tbs.swapaxes(-2, -1) @ (weights[None, :, None] * Tbs)
        #all_y = (Tbs.swapaxes(-2, -1) @ wflux)
        ###!!! NOTE - uncomment the below line to run (B)
        #(all_M, all_y) = calc_M_y_batch(Tbs, weights, wflux, nz, nbasis)
        ###!!! NOTE - uncomment the 2 lines below to run (C)
        all_M = calc_batch_dot_product_3d3d_gpu(Tbs, (weights[None, :, None] * Tbs), transpose_a=True)
        all_y = (Tbs.swapaxes(-2, -1) @ wflux)
        ###!!! NOTE - uncomment the 2 lines below to run an alternative
        ###    version of (C) that does the transpose on the CPU - this seems
        ###    to needlessly waste time though
        #all_M = calc_batch_dot_product_3d3d_gpu(cp.ascontiguousarray(Tbs.swapaxes(-2, -1)), (weights[None, :, None] * Tbs))
        #all_y = (Tbs.swapaxes(-2, -1) @ wflux)

        #3) Use cupy linalg.solve to solve for zcoeff in batch for all_M and
        #all_y.  There is no Error thrown by cupy's version.
        zcoeff = cp.linalg.solve(all_M, all_y)

        #4) calc_batch_dot_product_3d2d will compute the dot product
        #of Tbs and zcoeff for all templates in parallel.
        #It is the same as model[i,:,:] = Tbs[i,:,:].dot(zcoeff[i,:])
        model = calc_batch_dot_product_3d2d(Tbs, zcoeff, use_gpu)

        #5) On the GPU, (flux-model)*(flux-model) is faster than
        #(flux-model)**2.  The @ matrix multiplication operator performs
        #a dot product for each template.  get() copies the data back
        #from the GPU to the numpy array allocated for zchi2.
        zchi2[:] = (((flux - model)*(flux-model)) @ weights).get()
        #Copy data from GPU to numpy arrays
        zcoeff = zcoeff.get()
    else:
        zcoeff = np.zeros((nz, nbasis))
        #On the CPU, the templates are looped over and all operations
        #are performed on one template at a time.
        for i in range(nz):
            #1) dot_product_sparse_one will compute dot products of all
            #spectra with ONE template and return a 2D array of size
            #(ncols x nbasis)
            Tb = dot_product_sparse_one(spectra, tdata[i])

            #2) On the CPU, M and y are computed for each template
            M = Tb.T.dot(np.multiply(weights[:,None], Tb))
            y = Tb.T.dot(wflux)

            #3) Use numpy linalg.solve to solve for zcoeff for each M, y
            #LinAlgError must be caught
            try:
                zcoeff[i,:] = np.linalg.solve(M, y)
            except np.linalg.LinAlgError:
                zchi2[i] = 9e99
                continue

            #4) Calculate dot products individually for each template
            model = Tb.dot(zcoeff[i,:])

            #5) Calculate this zchi2 element individually for each template
            zchi2[i] = np.dot( (flux - model)**2, weights )
    return (zchi2, zcoeff)


###!!! NOTE - this is the main method for the v3 algorithm
###    In this version, everything is done in batch on the GPU but the
###    templates are looped over on the CPU.  The operations performed
###    are very obviously analagous though and should be highly
###    maintainable.  The main difference is the extra loop on the CPU version
def calc_zchi2_v3(target_ids, target_data, dtemplate, progress=None, use_gpu=False):
    """Calculate chi2 vs. redshift for a given PCA template.

    New CPU/GPU algorithms June 2022

    Args:
        target_ids (list): targets IDs.
        target_data (list): list of Target objects.
        dtemplate (DistTemplate): distributed template data
        progress (multiprocessing.Queue): optional queue for tracking
            progress, only used if MPI is disabled.

    Returns:
        tuple: (zchi2, zcoeff, zchi2penalty) with:
            - zchi2[ntargets, nz]: array with one element per target per
                redshift
            - zcoeff[ntargets, nz, ncoeff]: array of best fit template
                coefficients for each target at each redshift
            - zchi2penalty[ntargets, nz]: array of penalty priors per target
                and redshift, e.g. to penalize unphysical fits

    """
    nz = len(dtemplate.local.redshifts)
    ntargets = len(target_ids)
    nbasis = dtemplate.template.nbasis

    zchi2 = np.zeros( (ntargets, nz) )
    zchi2penalty = np.zeros( (ntargets, nz) )
    zcoeff = np.zeros( (ntargets, nz, nbasis) )

    # Redshifts near [OII]; used only for galaxy templates
    if dtemplate.template.template_type == 'GALAXY':
        isOII = (3724 <= dtemplate.template.wave) & \
            (dtemplate.template.wave <= 3733)
        OIItemplate = dtemplate.template.flux[:,isOII].T

    # Combine redshifted templates into CUPY arrays if using GPU
    if (use_gpu):
        tdata = dict()
        for key in dtemplate.local.data[0].keys():
            tdata[key] = cp.array([tdata[key] for tdata in dtemplate.local.data])
    else:
        #For CPU, pass through data as-is
        tdata = dtemplate.local.data

    for j in range(ntargets):
        (weights, flux, wflux) = spectral_data(target_data[j].spectra)
        if np.sum(weights) == 0:
            zchi2[j,:] = 9e99
            continue
        if (use_gpu):
            #Copy data to CUPY arrays
            weights = cp.array(weights)
            flux = cp.array(flux)
            wflux = cp.array(wflux)

        # Solving for template fit coefficients for all redshifts.
        # We use the pre-interpolated templates for each
        # unique wavelength range.

        # Use helper method calc_zchi2_batch to calculate zchi2 and zcoeff
        # for all templates for all three spectra for this target
        (zchi2[j,:], zcoeff[j,:,:]) = calc_zchi2_batch_v3(target_data[j].spectra, tdata, weights, flux, wflux, nz, nbasis, use_gpu)

        #- Penalize chi2 for negative [OII] flux; ad-hoc
        if dtemplate.template.template_type == 'GALAXY':
            OIIflux = np.sum(zcoeff[j] @ OIItemplate.T, axis=1)
            zchi2penalty[j][OIIflux < 0] = -OIIflux[OIIflux < 0]

        if dtemplate.comm is None:
            progress.put(1)

    return zchi2, zcoeff, zchi2penalty

###!!! NOTE - this is the original GPU implementation and will be removed
###    in the final version
def calc_zchi2_gpu(target_ids, target_data, dtemplate, progress=None):
    """Calculate chi2 vs. redshift for a given PCA template.

    Args:
        target_ids (list): targets IDs.
        target_data (list): list of Target objects.
        dtemplate (DistTemplate): distributed template data
        progress (multiprocessing.Queue): optional queue for tracking
            progress, only used if MPI is disabled.

    Returns:
        tuple: (zchi2, zcoeff, zchi2penalty) with:
            - zchi2[ntargets, nz]: array with one element per target per
                redshift
            - zcoeff[ntargets, nz, ncoeff]: array of best fit template
                coefficients for each target at each redshift
            - zchi2penalty[ntargets, nz]: array of penalty priors per target
                and redshift, e.g. to penalize unphysical fits

    """
    nz = len(dtemplate.local.redshifts)
    ntargets = len(target_ids)
    nbasis = dtemplate.template.nbasis

    zchi2 = cp.zeros( (ntargets, nz) )
    zchi2penalty = cp.zeros( (ntargets, nz) )
    zcoeff = cp.zeros( (ntargets, nz, nbasis) )

    # Redshifts near [OII]; used only for galaxy templates
    if dtemplate.template.template_type == 'GALAXY':
        isOII = (3724 <= dtemplate.template.wave) & \
            (dtemplate.template.wave <= 3733)
        OIItemplate = cp.array(dtemplate.template.flux[:,isOII].T)

    # Combine redshifted templates
    tdata = dict()
    for key in dtemplate.local.data[0].keys():
        tdata[key] = cp.array([tdata[key] for tdata in dtemplate.local.data])

    for j in range(ntargets):
        (weights, flux, wflux) = spectral_data(target_data[j].spectra)
        if np.sum(weights) == 0:
            zchi2[j] = 9e99
            continue
        weights = cp.array(weights)
        flux = cp.array(flux)
        wflux = cp.array(wflux)

        # Solving for template fit coefficients for all redshifts.
        # We use the pre-interpolated templates for each
        # unique wavelength range.
        Tbs = []
        for s in target_data[j].spectra:
            key = s.wavehash
            R = cupyx.scipy.sparse.csr_matrix(s.Rcsr).toarray()
            Tbs.append(cp.einsum('mn,jnk->jmk', R, tdata[key]))
        Tbs = cp.concatenate(Tbs, axis=1)
        cp.cuda.Stream.null.synchronize()
        zchi2[j] = _zchi2_batch(Tbs, weights, flux, wflux, zcoeff[j])

        #- Penalize chi2 for negative [OII] flux; ad-hoc
        if dtemplate.template.template_type == 'GALAXY':
            OIIflux = np.sum(zcoeff[j] @ OIItemplate.T, axis=1)
            zchi2penalty[j][OIIflux < 0] = -OIIflux[OIIflux < 0]

        if dtemplate.comm is None:
            progress.put(1)

    return zchi2.get(), zcoeff.get(), zchi2penalty.get()


###!!! NOTE - this is the v1 GPU implementation, modified to time all
###    of the custom kernels versus their CUPY implementations.  It will be
###    removed in the final version
def calc_zchi2_gpu_new(target_ids, target_data, dtemplate, progress=None):
    """Calculate chi2 vs. redshift for a given PCA template.

    New GPU algorithms 4/22/22

    Args:
        target_ids (list): targets IDs.
        target_data (list): list of Target objects.
        dtemplate (DistTemplate): distributed template data
        progress (multiprocessing.Queue): optional queue for tracking
            progress, only used if MPI is disabled.

    Returns:
        tuple: (zchi2, zcoeff, zchi2penalty) with:
            - zchi2[ntargets, nz]: array with one element per target per
                redshift
            - zcoeff[ntargets, nz, ncoeff]: array of best fit template
                coefficients for each target at each redshift
            - zchi2penalty[ntargets, nz]: array of penalty priors per target
                and redshift, e.g. to penalize unphysical fits

    """
    nz = len(dtemplate.local.redshifts)
    ntargets = len(target_ids)
    nbasis = dtemplate.template.nbasis

    zchi2 = np.zeros( (ntargets, nz) )
    zchi2penalty = np.zeros( (ntargets, nz) )
    zcoeff = np.zeros( (ntargets, nz, nbasis) )
    modes = [0,0,0,0]

    block_size = 512 #Default block size, should work on all modern nVidia GPUs
    # Load CUDA kernels
    cp_module = cp.RawModule(code=cuda_source)
    batch_dot_product_sparse = cp_module.get_function('batch_dot_product_sparse')
    calc_M_y = cp_module.get_function('calc_M_and_y_atomic')
    batch_dot_product_3d2d = cp_module.get_function('batch_dot_product_3d2d')
    calc_z_prod = cp_module.get_function('calc_z_prod')
    tx = 0

    # Redshifts near [OII]; used only for galaxy templates
    if dtemplate.template.template_type == 'GALAXY':
        isOII = (3724 <= dtemplate.template.wave) & \
            (dtemplate.template.wave <= 3733)
        OIItemplate = np.array(dtemplate.template.flux[:,isOII].T)

    # Combine redshifted templates
    tdata = dict()
    for key in dtemplate.local.data[0].keys():
        tdata[key] = cp.array([tdata[key] for tdata in dtemplate.local.data])

    for j in range(ntargets):
        (weights, flux, wflux) = spectral_data(target_data[j].spectra)
        if np.sum(weights) == 0:
            zchi2[j,:] = 9e99
            continue
        weights = cp.array(weights)
        flux = cp.array(flux)
        wflux = cp.array(wflux)

        # Solving for template fit coefficients for all redshifts.
        # We use the pre-interpolated templates for each
        # unique wavelength range.
        Tbs = []
        for s in target_data[j].spectra:
            key = s.wavehash
            #R = cupyx.scipy.sparse.csr_matrix(s.Rcsr).toarray()

            #Use actual numpy arrays that represent sparse array - .data, .indices, and .indptr
            #Use batch_dot_product_sparse array to perform dot product in parallel for all templates
            #for this (target, spectrum) combination.
            #Allocate CUPY arrays and calculate number of blocks to use.
            if (modes[0] == 0):
                n = tdata[key].size
                blocks = (n+block_size-1)//block_size
                Rcsr_values = cp.array(s.Rcsr.data, cp.float64)
                Rcsr_cols = cp.array(s.Rcsr.indices, cp.int32)
                Rcsr_indptr = cp.array(s.Rcsr.indptr, cp.int32)
                #Array dimensions
                nrows = cp.int32(s.Rcsr.shape[1])
                ncols = cp.int32(s.Rcsr.shape[0])
                nbasis = cp.int32(tdata[key].shape[2])
                nt = cp.int32(tdata[key].shape[0])
                curr_tb = cp.empty((nz, ncols, nbasis))
                #Launch kernel and syncrhronize
                batch_dot_product_sparse((blocks,), (block_size,), (Rcsr_values, Rcsr_cols, Rcsr_indptr, tdata[key], curr_tb, nrows, ncols, nbasis, nt))
            else:
                nt = cp.int32(tdata[key].shape[0])
                n = tdata[key].size
                nbasis = cp.int32(tdata[key].shape[2])
                #curr_tb = s.Rcsr.dot(tdata[key])
                R = cupyx.scipy.sparse.csr_matrix(s.Rcsr).toarray()
                curr_tb = cp.einsum('mn,jnk->jmk', R, tdata[key])
            #Commented out synchronize - needed for timing kernels but we still
            #get execution of this kernel before data is needed for next kernel
            #so output is the same and slightly faster without synchronize
            #cp.cuda.Stream.null.synchronize()
            #Append to list
            Tbs.append(curr_tb)
        #Use CUPY.hstack to combine into one nt x ncols x nbasis array
        Tbs = cp.hstack(Tbs)
        #cp.cuda.Stream.null.synchronize()

        #Use calc_M_y_atomic kernel to compute M and y arrays
        #nparallel - number of parallel threads for each output array element
        #For larger input Tbs arrays - e.g., GALAXY, QSO, 4 parallel threads
        #is faster because we don't want to create too many total threads
        #But for smaller Tb arrays - STARS - we can use more parallel threads
        #to maximize parallelism - this can be dynamically tuned but in test
        #data, 4 and 64 were optimal.  Needs to be power of 2.
        if (modes[1] == 0):
            if (nt > 512):
                nparallel = cp.int32(4)
            else:
                nparallel = cp.int32(64)
            #Create CUPY arrays and calculate number of blocks
            nrows = cp.int32(Tbs.shape[1])
            n = nt*nbasis*nbasis*nparallel + nt*nbasis*nparallel
            blocks = (n+block_size-1)//block_size
            #need to allocate first iteration through loop
            #provided the shape is constant which it should be
            #all elements will be overwritten by kernel each iteration
            #empty is super fast but this saves us time on cudaFree
            if (j == 0):
                all_M = cp.zeros((nt, nbasis, nbasis))
                all_y = cp.zeros((nt, nbasis))
            else:
                all_M[:] = 0
                all_y[:] = 0
            #Launch kernel and syncrhonize
            calc_M_y((blocks,), (block_size,), (Tbs, weights, wflux, all_M, all_y, nrows, nbasis, nt, nparallel))
            #Commented out synchronize - needed for timing kernels but we still
            #get execution of this kernel before data is needed for next kernel
            #so output is the same and slightly faster without synchronize
            #cp.cuda.Stream.null.synchronize()

        if (modes[1] == 1):
            ### CUPY ####
            #Commented out because above code is slightly faster but leaving for
            #future reference because this is simpler code that does the same
            all_M = Tbs.swapaxes(-2, -1) @ (weights[None, :, None] * Tbs)
            all_y = (Tbs.swapaxes(-2, -1) @ wflux)
            #cp.cuda.Stream.null.synchronize()

        #bool array to track elements with LinAlgError from np.linalg.solve
        zc = cp.linalg.solve(all_M, all_y)

        #Use batch_dot_product_3d2d kernel to computer model array
        #Allocate CUPY array and calc blocks to be used
        if (modes[2] == 0):
            nrows = cp.int32(Tbs.shape[1])
            n = nrows * nt
            blocks = (n+block_size-1)//block_size
            if (j == 0):
                #Again only allocate first iteration through loop
                model = cp.empty((nt, nrows), cp.float64)
            #Launch kernel and synchronize
            batch_dot_product_3d2d((blocks,), (block_size,), (Tbs, zc, model, nrows, nbasis, nt))
            #cp.cuda.Stream.null.synchronize()
        else:
            model = cp.squeeze((Tbs @ zc[:, :, None]))

        #Use calc_z_prod kernel to calculate all zchi2 for this target in parallel
        #Allocate temp array to hold results - blocks is the same as in batch_dot_product_3d2d kernel above.
        if (modes[3] == 0):
            nrows = cp.int32(Tbs.shape[1])
            n = nrows * nt
            blocks = (n+block_size-1)//block_size
            if (j == 0):
                #Again only allocate first iteration through loop
                z_product = cp.empty((nt, nrows), cp.float64)
            #Launch kernel
            calc_z_prod((blocks,), (block_size,), (flux, model, weights, z_product, nrows, nt))
            #cp.cuda.Stream.null.synchronize()
            zchi2[j,:] = z_product.sum(1).get()
        else:
            zchi2[j,:] = (((flux - model)*(flux-model)) @ weights).get()
            #cp.cuda.Stream.null.synchronize()
        #Copy data from GPU to numpy arrays
        zcoeff[j,:,:] = zc.get()
        #Free data from GPU
        del zc
        del Tbs
        #Moved freeing these to after loop, only allocate and free once
        #del model
        #del z_product
        #del all_M
        #del all_y

        #- Penalize chi2 for negative [OII] flux; ad-hoc
        if dtemplate.template.template_type == 'GALAXY':
            OIIflux = np.sum(zcoeff[j] @ OIItemplate.T, axis=1)
            zchi2penalty[j][OIIflux < 0] = -OIIflux[OIIflux < 0]

        if dtemplate.comm is None:
            progress.put(1)
    #Free all_M and all_y here since only allocating once
    del all_M
    del all_y
    del model
    if (modes[3] == 0):
        del z_product

    return zchi2, zcoeff, zchi2penalty

def _mp_calc_zchi2(indx, target_ids, target_data, t, qout, qprog):
    """Wrapper for multiprocessing version of calc_zchi2.
    """
    try:
        # Unpack targets from shared memory
        for tg in target_data:
            tg.sharedmem_unpack()
        tzchi2, tzcoeff, tpenalty = calc_zchi2(target_ids, target_data, t,
            progress=qprog)
        qout.put( (indx, tzchi2, tzcoeff, tpenalty) )
    except:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
        lines = [ "MP calc_zchi2: {}".format(x) for x in lines ]
        print("".join(lines))
        sys.stdout.flush()


def calc_zchi2_targets(targets, templates, mp_procs=1, use_gpu=False):
    """Compute all chi2 fits for the local set of targets and collect.

    Given targets and templates distributed across a set of MPI processes,
    compute the coarse-binned chi^2 fit for all redshifts and our local set
    of targets.  Each process computes the fits for a slice of redshift range
    and then cycles through redshift slices by passing the interpolated
    templates along to the next process in order.

    Args:
        targets (DistTargets): distributed targets.
        templates (list): list of DistTemplate objects.
        mp_procs (int): if not using MPI, this is the number of multiprocessing
            processes to use.
        gpu (bool): (optional) use gpu for calc_zchi2

    Returns:
        dict: dictionary of results for each local target ID.

    """

    # Find most likely candidate redshifts by scanning over the
    # pre-interpolated templates on a coarse redshift spacing.

    # See if we are the process that should be printing stuff...
    am_root = False
    if targets.comm is None:
        am_root = True
    elif targets.comm.rank == 0:
        am_root = True

    # If we are not using MPI, our DistTargets object will have all the targets
    # on the main process.  In that case, we would like to distribute our
    # targets across multiprocesses.  Here we compute that distribution, if
    # needed.

    mpdist = None
    if targets.comm is None:
        mpdist = distribute_targets(targets.local(), mp_procs)

    results = dict()
    for tid in targets.local_target_ids():
        results[tid] = dict()

    if am_root:
        print("Computing redshifts")
        sys.stdout.flush()

    for t in templates:
        ft = t.template.full_type

        if am_root:
            print("  Scanning redshifts for template {}"\
                .format(t.template.full_type))
            sys.stdout.flush()

        start = elapsed(None, "", comm=targets.comm)

        # There are 2 parallelization techniques supported here (MPI and
        # multiprocessing).

        zchi2 = None
        zcoeff = None
        penalty = None

        if targets.comm is not None:
            # MPI case.
            # The following while-loop will cycle through the redshift slices
            # (one per MPI process) until all processes have computed the chi2
            # for all redshifts for their local targets.

            if am_root:
                sys.stdout.write("    Progress: {:3d} %\n".format(0))
                sys.stdout.flush()

            zchi2 = dict()
            zcoeff = dict()
            penalty = dict()

            mpi_prog_frac = 1.0
            prog_chunk = 10
            if t.comm is not None:
                mpi_prog_frac = 1.0 / t.comm.size
                if t.comm.size < prog_chunk:
                    prog_chunk = 100 // t.comm.size
            proglast = 0
            prog = 1

            done = False
            #CW 04/25/22 - when running in GPU mode, all non-GPU procs should
            #have 0 targets.  Set done to true in these cases so it skips the
            #while loop - this saves ~2s on 500 targets on 64 CPU / 4 GPU
            #and no need to call calc_zchi2 on empty target list
            if (len(targets.local_target_ids()) == 0):
                done = True
            while not done:
                # Compute the fit for our current redshift slice.
                tzchi2, tzcoeff, tpenalty = \
                    calc_zchi2(targets.local_target_ids(), targets.local(), t, use_gpu=use_gpu)

                # Save the results into a dict keyed on targetid
                tids = targets.local_target_ids()
                for i, tid in enumerate(tids):
                    if tid not in zchi2:
                        zchi2[tid] = {}
                        zcoeff[tid] = {}
                        penalty[tid] = {}
                    zchi2[tid][t.local.index] = tzchi2[i]
                    zcoeff[tid][t.local.index] = tzcoeff[i]
                    penalty[tid][t.local.index] = tpenalty[i]

                prg = int(100.0 * prog * mpi_prog_frac)
                if prg >= proglast + prog_chunk:
                    proglast += prog_chunk
                    if am_root and (t.comm is not None):
                        sys.stdout.write("    Progress: {:3d} %\n"\
                            .format(proglast))
                        sys.stdout.flush()
                prog += 1

                # Cycle through the redshift slices
                done = t.cycle()

            for tid in zchi2.keys():
                zchi2[tid] = np.concatenate([ zchi2[tid][p] for p in sorted(zchi2[tid].keys()) ])
                zcoeff[tid] = np.concatenate([ zcoeff[tid][p] for p in sorted(zcoeff[tid].keys()) ])
                penalty[tid] = np.concatenate([ penalty[tid][p] for p in sorted(penalty[tid].keys()) ])
        else:
            # Multiprocessing case.
            import multiprocessing as mp

            # Ensure that all targets are packed into shared memory
            for tg in targets.local():
                tg.sharedmem_pack()

            # We explicitly spawn processes here (rather than using a pool.map)
            # so that we can communicate the read-only objects once and send
            # a whole list of redshifts to each process.

            qout = mp.Queue()
            qprog = mp.Queue()

            procs = list()
            for i in range(mp_procs):
                if len(mpdist[i]) == 0:
                    continue
                target_ids = mpdist[i]
                target_data = [ x for x in targets.local() if x.id in mpdist[i] ]
                p = mp.Process(target=_mp_calc_zchi2,
                    args=(i, target_ids, target_data, t, qout, qprog))
                procs.append(p)
                p.start()

            # Track progress
            sys.stdout.write("    Progress: {:3d} %\n".format(0))
            sys.stdout.flush()
            ntarget = len(targets.local_target_ids())
            progincr = 10
            if mp_procs > ntarget:
                progincr = int(100.0 / ntarget)
            tot = 0
            proglast = 0
            while (tot < ntarget):
                cnt = qprog.get()
                tot += cnt
                prg = int(100.0 * tot / ntarget)
                if prg >= proglast + progincr:
                    proglast += progincr
                    sys.stdout.write("    Progress: {:3d} %\n".format(proglast))
                    sys.stdout.flush()

            # Extract the output
            zchi2 = dict()
            zcoeff = dict()
            penalty = dict()
            for _ in range(len(procs)):
                res = qout.get()
                tids = mpdist[res[0]]
                for j,tid in enumerate(tids):
                    zchi2[tid] = res[1][j]
                    zcoeff[tid] = res[2][j]
                    penalty[tid] = res[3][j]

        elapsed(start, "    Finished in", comm=targets.comm)

        for tid in sorted(zchi2.keys()):
            results[tid][ft] = dict()
            results[tid][ft]['redshifts'] = t.template.redshifts
            results[tid][ft]['zchi2'] = zchi2[tid]
            results[tid][ft]['penalty'] = penalty[tid]
            results[tid][ft]['zcoeff'] = zcoeff[tid]

    return results

