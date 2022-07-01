import functools
import logging

from vayesta.core.util import log_time


def scf_with_mpi(mpi, mf, mpi_rank=0, log=None):
    """Use to run SCF only on the master node and broadcast result afterwards."""

    if not mpi:
        return mf

    bcast = functools.partial(mpi.world.bcast, root=mpi_rank)
    kernel_orig = mf.kernel
    log = log or mpi.log or logging.getLogger(__name__)

    def mpi_kernel(self, *args, **kwargs):
        if mpi.rank == mpi_rank:
            log.info("MPI rank= %3d is running SCF", mpi.rank)
            with log_time(log.timing, "Time for SCF: %s"):
                res = kernel_orig(*args, **kwargs)
            log.info("MPI rank= %3d finished SCF", mpi.rank)
        else:
            res = None
            # Generate auxiliary cell, compensation basis etc,..., but not 3c integrals:
            if hasattr(self, 'with_df') and self.with_df.auxcell is None:
                self.with_df.build(with_j3c=False)
            log.info("MPI rank= %3d is waiting for SCF results", mpi.rank)
        mpi.world.barrier()

        # Broadcast results
        with log_time(log.timing, "Time for MPI broadcast of SCF results: %s"):
            res = bcast(res)
            if hasattr(self, 'with_df'):
                self.with_df._cderi = bcast(self.with_df._cderi)
            self.converged = bcast(self.converged)
            self.e_tot = bcast(self.e_tot)
            self.mo_energy = bcast(self.mo_energy)
            self.mo_occ = bcast(self.mo_occ)
            self.mo_coeff = bcast(self.mo_coeff)
        return res

    mf.kernel = mpi_kernel.__get__(mf)
    mf.with_mpi = True

    return mf
