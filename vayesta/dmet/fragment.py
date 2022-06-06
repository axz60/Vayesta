import dataclasses
from timeit import default_timer as timer

# External libaries
import numpy as np

from vayesta.core.qemb import Fragment
from vayesta.core.bath import BNO_Threshold
from vayesta.solver import get_solver_class
from vayesta.core.util import *


# We might want to move the useful things from here into core, since they seem pretty general.

class DMETFragmentExit(Exception):
    pass

VALID_SOLVERS = [None, "", "MP2", "CISD", "CCSD", "CCSD(T)", 'FCI', "FCI-spin0", "FCI-spin1"]

@dataclasses.dataclass
class Options(Fragment.Options):
    pass

@dataclasses.dataclass
class Results(Fragment.Results):
    n_active: int = None
    e1: float = None
    e2: float = None

    @property
    def dm1(self):
        """Cluster 1DM"""
        return self.wf.make_rdm1()

    @property
    def dm2(self):
        """Cluster 2DM"""
        return self.wf.make_rdm2()


class DMETFragment(Fragment):

    Options = Options
    Results = Results

    def __init__(self, *args, solver=None, **kwargs):

        """
        Parameters
        ----------
        base : DMET
            Base DMET object.
        fid : int
            Unique ID of fragment.
        name :
            Name of fragment.
        """

        super().__init__(*args, **kwargs)

        if solver is None:
            solver = self.base.solver
        self.check_solver(solver)
        self.solver = solver
        self.solver_results = None

    def check_solver(self, solver):
        if solver not in VALID_SOLVERS:
            raise ValueError("Unknown solver: %s" % solver)

    def kernel(self, solver=None, init_guess=None, eris=None, construct_bath=True, chempot=None):
        """Run solver for a single BNO threshold.

        Parameters
        ----------
        solver : {'MP2', 'CISD', 'CCSD', 'CCSD(T)', 'FCI'}, optional
            Correlated solver.

        Returns
        -------
        results : DMETFragmentResults
        """

        if solver is None:
            solver = self.solver
        if self._dmet_bath is None or construct_bath:
            self.make_bath()

        cluster = self.make_cluster()
        # If we want to reuse previous info for initial guess and eris we'd do that here...
        # We can now overwrite the orbitals from last BNO run:
        self._c_active_occ = cluster.c_active_occ
        self._c_active_vir = cluster.c_active_vir

        if solver is None:
            return None

        # Create solver object
        solver_cls = get_solver_class(self.mf, solver)
        solver_opts = self.get_solver_options(solver)
        cluster_solver = solver_cls(self.mf, self, cluster, **solver_opts)
        # Chemical potential
        if chempot is not None:
            cluster_solver.v_ext = -chempot * self.get_fragment_projector(self.cluster.c_active)
        if eris is None:
            eris = cluster_solver.get_eris()
        with log_time(self.log.info, ("Time for %s solver:" % solver) + " %s"):
            cluster_solver.kernel(eris=eris)

        results = self._results
        results.wf = cluster_solver.wf
        results.n_active = self.cluster.norb_active

        results.e1, results.e2 = self.get_dmet_energy_contrib()

        return results

    def get_solver_options(self, solver):
        solver_opts = {}
        solver_opts.update(self.opts.solver_options)
        return solver_opts

    def get_dmet_energy_contrib(self, eris=None):
        """Calculate the contribution of this fragment to the overall DMET energy."""
        # Projector to the impurity in the active basis.
        P_imp = self.get_fragment_projector(self.cluster.c_active)
        c_act = self.cluster.c_active
        if eris is None:
            with log_time(self.log.timing, "Time for AO->MO transformation: %s"):
                eris = self.base.get_eris_array(c_act)
        nocc = self.cluster.c_active_occ.shape[1]
        occ = np.s_[:nocc]
        # Calculate the effective onebody interaction within the cluster.
        f_act = np.linalg.multi_dot((c_act.T, self.mf.get_fock(), c_act))
        v_act = 2 * np.einsum('iipq->pq', eris[occ, occ]) - np.einsum('iqpi->pq', eris[occ, :, :, occ])
        h_eff = f_act - v_act
        h_bare = np.linalg.multi_dot((c_act.T, self.base.get_hcore(), c_act))

        e1 = 0.5 * dot(P_imp, h_bare + h_eff, self.results.dm1).trace()
        e2 = 0.5 * np.einsum('pt,tqrs,pqrs->', P_imp, eris, self.results.dm2)
        # Code to generate the HF energy contribution for testing purposes.
        # mf_dm1 = np.linalg.multi_dot((c_act.T, self.base.get_ovlp(), self.mf.make_rdm1(),\
        #                               self.base.get_ovlp(), c_act))
        # e_hf = np.linalg.multi_dot((P_imp, 0.5 * (h_bare + f_act), mf_dm1)).trace()
        return e1, e2

    def get_frag_hl_dm(self):
        c = dot(self.c_frag.T, self.mf.get_ovlp(), self.cluster.c_active)
        return dot(c, self.results.dm1, c.T)

    def get_nelectron_hl(self):
        return self.get_frag_hl_dm().trace()

    def get_energy_prefactor(self):
        # Defined for compatibility..
        return 1.0
