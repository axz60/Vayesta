from .solver import ClusterSolver, UClusterSolver
from .hamiltonian import is_uhf_ham, is_eb_ham

from vayesta.core.types import CISD_WaveFunction
from pyscf import ci
from ._uccsd_eris import uao2mo

def CISD_Solver(hamil, *args, **kwargs):
    if is_eb_ham(hamil):
        raise NotImplementedError("Coupled electron-boson CISD solver not implemented.")
    if is_uhf_ham(hamil):
        return UCISD_Solver(hamil, *args, **kwargs)
    else:
        return RCISD_Solver(hamil, *args, **kwargs)


class RCISD_Solver(ClusterSolver):

    def kernel(self, *args, **kwargs):
        mf_clus, frozen = self.hamil.to_pyscf_mf(allow_dummy_orbs=True)
        solver_class = self.get_solver_class()
        mycisd = solver_class(mf_clus, frozen=frozen)
        ecisd, civec = mycisd.kernel()
        c0, c1, c2 = mycisd.cisdvec_to_amplitudes(civec)
        self.wf = CISD_WaveFunction(self.hamil.mo, c0, c1, c2)
        self.converged = True

    def get_solver_class(self):
        return ci.RCISD


class UCISD_Solver(UClusterSolver, RCISD_Solver):
    def get_solver_class(self):
        return UCISD


class UCISD(ci.ucisd.UCISD):
    ao2mo = uao2mo