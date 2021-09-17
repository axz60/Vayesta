import dataclasses
import copy

import numpy as np
import scipy.linalg

import pyscf
import pyscf.lib
import pyscf.agf2

import vayesta
from vayesta.ewf import helper
from vayesta.core import QEmbeddingMethod
from vayesta.core.util import time_string
from vayesta.eagf2.fragment import EAGF2Fragment
from vayesta.eagf2.ragf2 import RAGF2, RAGF2Options, DIIS

try:
    from mpi4py import MPI
    timer = MPI.Wtime
except ImportError:
    from timeit import default_timer as timer


@dataclasses.dataclass
class EAGF2Options(RAGF2Options):
    ''' Options for EAGF2 calculations - see `RAGF2Options`.
    '''

    # --- Fragment settings
    fragment_type: str = 'Lowdin-AO'
    iao_minao: str = 'auto'

    # --- Bath settings
    bath_type: str = 'POWER'    # 'MP2-BNO', 'POWER', 'ALL', 'NONE'
    max_bath_order: int = 2
    bno_threshold: float = 1e-8
    bno_threshold_factor: float = 1.0
    dmet_threshold: float = 1e-4

    # --- Other
    strict: bool = False
    orthogonal_mo_tol: float = 1e-10
    recalc_veff: bool = False
    copy_mf: bool = False


@dataclasses.dataclass
class EAGF2Results:
    ''' Results for EAGF2 calculations.

    Attributes
    ----------
    converged : bool
        Convergence flag.
    e_corr : float
        Correlation energy.
    e_1b : float
        One-body part of total energy, including nuclear repulsion.
    e_2b : float
        Two-body part of total energy.
    gf: pyscf.agf2.GreensFunction
        Green's function object.
    se: pyscf.agf2.SelfEnergy
        Self-energy object.
    solver: RAGF2
        RAGF2 solver object.
    '''

    converged: bool = None
    e_corr: float = None
    e_1b: float = None
    e_2b: float = None
    gf: pyscf.agf2.GreensFunction = None
    se: pyscf.agf2.SelfEnergy = None
    solver: RAGF2 = None


class EAGF2(QEmbeddingMethod):

    Options = EAGF2Options
    Results = EAGF2Results
    Fragment = EAGF2Fragment
    DIIS = DIIS

    def __init__(self, mf, options=None, log=None, **kwargs):
        ''' Embedded AGF2 calculation.

        Parameters
        ----------
        mf : pyscf.scf ojbect
            Converged mean-field object.
        options : EAGF2Options
            Options `dataclass`.
        log : logging.Logger
            Logger object. If None, the default Vayesta logger is used
            (default value is None).
        fragment_type : {'Lowdin-AO', 'IAO'}
            Fragmentation method (default value is 'Lowdin-AO').
        iao_minao : str
            Minimal basis for IAOs (default value is 'auto').
        bath_type : {'MP2-BNO', 'POWER', 'ALL', 'NONE'}
            Bath orbital method (default value is 'POWER').
        max_bath_order : int
            Maximum order of power orbitals (default value is 2).
        bno_threshold : float
            Threshold for BNO cutoff when `bath_type` is 'MP2-BNO'
            (default value is 1e-8).
        bno_threshold_factor : float
            Additional factor for `bno_threshold` (default value is 1).
        dmet_threshold : float
            Threshold for idempotency of cluster DM in DMET bath
            construction (default value is 1e-4).
        strict : bool
            Force convergence in the mean-field calculations (default
            value is True).
        orthogonal_mo_tol : float
            Threshold for orthogonality in molecular orbitals (default
            value is 1e-9).

        Plus any keyword argument from RAGF2Options.

        Attributes
        ----------
        results : EAGF2Results
            Results of EAGF2 calculation, see `EAGF2Results` for a list
            of attributes.
        e_tot : float
            Total energy.
        e_corr : float
            Correlation energy.
        e_ip : float
            Ionisation potential.
        e_ea : float
            Electron affinity.
        '''

        super().__init__(mf, log=log)
        t0 = timer()

        # --- Options for EAGF2
        self.opts = options
        if self.opts is None:
            self.opts = EAGF2Options(**kwargs)
        else:
            self.opts = self.opts.replace(kwargs)
        self.log.info("EAGF2 parameters:")
        for key, val in self.opts.items():
            self.log.info("  > %-24s %r", key + ":", val)

        # --- Check input
        if not mf.converged:
            if self.opts.strict:
                raise RuntimeError("Mean-field calculation not converged.")
            else:
                self.log.error("Mean-field calculation not converged.")

        # --- Orthogonalize insufficiently orthogonal MOs
        # (For example as a result of k2gamma conversion with low cell.precision)
        c = self.mo_coeff.copy()
        assert np.all(c.imag == 0), "max|Im(C)|= %.2e" % abs(c.imag).max()
        ctsc = np.linalg.multi_dot((c.T, self.get_ovlp(), c))
        nonorth = abs(ctsc - np.eye(ctsc.shape[-1])).max()
        self.log.info("Max. non-orthogonality of input orbitals= %.2e%s", nonorth,
                      " (!!!)" if nonorth > 1e-5 else "")
        if self.opts.orthogonal_mo_tol and nonorth > self.opts.orthogonal_mo_tol:
            t0 = timer()
            self.log.info("Orthogonalizing orbitals...")
            self.mo_coeff = helper.orthogonalize_mo(c, self.get_ovlp())
            change = abs(np.diag(np.linalg.multi_dot((self.mo_coeff.T, self.get_ovlp(), c)))-1)
            self.log.info("Max. orbital change= %.2e%s", change.max(),
                          " (!!!)" if change.max() > 1e-4 else "")
            self.log.timing("Time for orbital orthogonalization: %s", time_string(timer()-t0))

        # --- Prepare fragments
        t1 = timer()
        fragkw = {}
        if self.opts.fragment_type.upper() == 'IAO':
            raise NotImplementedError("IAOs are not yet supported for EAGF2")
            if self.opts.iao_minao == 'auto':
                self.opts.iao_minao = helper.get_minimal_basis(self.mol.basis)
                self.log.warning("Minimal basis set '%s' for IAOs was selected automatically.",
                                 self.opts.iao_minao)
            self.log.info("Computational basis= %s", self.mol.basis)
            self.log.info("Minimal basis=       %s", self.opts.iao_minao)
            fragkw['minao'] = self.opts.iao_minao
        self.init_fragmentation(self.opts.fragment_type, **fragkw)
        self.symfrags = []
        self.log.timing("Time for fragment initialization: %s", time_string(timer() - t1))

        self.log.timing("Time for EAGF2 setup: %s", time_string(timer() - t0))

        self.cluster_results = {}
        self.results = None


    @property
    def e_tot(self):
        return self.results.e_1b + self.results.e_2b

    @property
    def e_corr(self):
        return self.results.e_corr

    @property
    def e_ip(self):
        return -self.results.gf.get_occupied().energy.max()

    @property
    def e_ea(self):
        return self.results.gf.get_virtual().energy.min()

    @property
    def converged(self):
        return self.results.converged


    def kernel(self):
        ''' Run the EAGF2 calculation.

        Returns
        -------
        results : EAGF2Results
            Object containing results of `EAGF2` calculation, see
            `EAGF2Results` for a list of attributes.
        '''

        t0 = timer()

        if self.nfrag == 0:
            raise ValueError("No fragments defined for calculation.")

        nelec_frags = sum([f.sym_factor*f.nelectron for f in self.loop()])
        self.log.info("Total number of mean-field electrons over all fragments= %.8f", nelec_frags)
        if abs(nelec_frags - np.rint(nelec_frags)) > 1e-4:
            self.log.warning("Number of electrons not integer!")

        self.log.info("Initialising solver:")
        self.log.changeIndentLevel(1)
        solver = RAGF2(
                self.mf,
                eri=np.empty(()),
                veff=np.empty(()),
                log=self.log,
                options=self.opts,
                fock_basis='ao',
        )
        solver.log = self.log
        self.log.changeIndentLevel(-1)

        diis = self.DIIS(space=self.opts.diis_space, min_space=self.opts.diis_min_space)
        solver.se = pyscf.agf2.aux.SelfEnergy(np.empty((0)), np.empty((solver.nact, 0)))
        fock = np.diag(self.mf.mo_energy)
        fock_mo = fock.copy()

        converged = False
        for niter in range(0, self.opts.max_cycle+1):
            t1 = timer()
            self.log.info("Iteration %d" % niter)
            self.log.info("**********%s" % ('*'*len(str(niter))))
            self.log.changeIndentLevel(1)

            se_prev = copy.deepcopy(solver.se)
            e_prev = solver.e_tot

            moms_demo = 0
            for x, frag in enumerate(self.fragments):
                self.log.info("Fragment %d" % x)
                self.log.info("---------%s" % ('-'*len(str(x))))
                self.log.changeIndentLevel(1)

                n = frag.c_frag.shape[0]
                sc = np.dot(self.get_ovlp(), self.mf.mo_coeff)
                c = np.dot(sc.T.conj(), frag.c_frag)
                p_frag = np.zeros((solver.nact+solver.se.naux, solver.nact+solver.se.naux))
                p_frag[:n, :n] += np.dot(c, c.T.conj())
                c_frag = scipy.linalg.orth(p_frag)
                c_env = scipy.linalg.null_space(p_frag)
                c_full = np.hstack((c_frag, c_env))
                assert c_env.shape[-1] == (solver.nact + solver.se.naux - c_frag.shape[1])

                if frag.sym_parent is None:
                    results = frag.kernel(solver, solver.se, fock, c_frag=c_frag, c_env=c_env)
                    c_active = results.c_active
                    self.cluster_results[frag.id] = results
                    self.log.info("%s is done.", frag)
                else:
                    self.log.info("Fragment is symmetry related, parent: %s", frag.sym_parent)
                    results = self.cluster_results[frag.sym_parent.id]

                p_frag = np.linalg.multi_dot((c_active.T.conj(), c_frag, c_frag.T.conj(), c_active))
                p_full = np.linalg.multi_dot((c_active.T.conj(), c_full, c_full.T.conj(), c_active))
                moms = frag.democratic_partition(results.moms, p1=p_frag, p2=p_full)

                c = results.c_active[:solver.nact].T.conj()
                moms_demo += np.einsum('...pq,pi,qj->...ij', moms, c, c)

                self.log.changeIndentLevel(-1)

            se_occ, se_vir = (solver._build_se_from_moments(m) for m in moms_demo)
            solver.se = solver._combine_se(se_occ, se_vir)

            if niter != 0:
                solver.run_diis(solver.se, None, diis, se_prev=se_prev)

            w, v = solver.solve_dyson(fock=fock_mo)
            solver.gf = pyscf.agf2.aux.GreensFunction(w, v[:solver.nact])
            solver.gf, solver.se, fconv, fock = solver.fock_loop(fock=fock_mo, return_fock=True)
            solver.gf.remove_uncoupled(tol=1e-12)

            solver.e_1b = solver.energy_1body()
            solver.e_2b = solver.energy_2body()
            solver.print_energies()
            solver.print_excitations()

            deltas = solver._convergence_checks(se=solver.se, se_prev=se_prev, e_prev=e_prev)

            self.log.info("Change in energy:     %10.3g", deltas[0])
            self.log.info("Change in 0th moment: %10.3g", deltas[1])
            self.log.info("Change in 1st moment: %10.3g", deltas[2])

            if self.opts.dump_chkfile and solver.chkfile is not None:
                self.log.debug("Dumping current iteration to chkfile")
                solver.dump_chk()

            self.log.timing("Time for AGF2 iteration:  %s", time_string(timer() - t1))

            self.log.changeIndentLevel(-1)

            if deltas[0] < self.opts.conv_tol \
                    and deltas[1] < self.opts.conv_tol_t0 \
                    and deltas[2] < self.opts.conv_tol_t1:
                converged = True
                break

        solver.gf.remove_uncoupled(tol=1e-12)
        solver.e_1b = solver.energy_1body()
        solver.e_2b = solver.energy_2body()

        self.results = EAGF2Results(
                converged=converged,
                e_corr=solver.e_corr,
                e_1b=solver.e_1b,
                e_2b=solver.e_2b,
                gf=solver.gf,
                se=solver.se,
                solver=solver,
        )

        (self.log.info if converged else self.log.warning)("Converged = %r", converged)

        if self.opts.pop_analysis:
            solver.population_analysis()

        if self.opts.dip_moment:
            solver.dip_moment()

        if self.opts.dump_cubefiles:
            #TODO test
            self.log.debug("Dumping orbitals to .cube files")
            gf_occ, gf_vir = solver.gf.get_occupied(), solver.gf.get_virtual()
            for i in range(self.opts.dump_cubefiles):
                if (gf_occ.naux-1-i) >= 0:
                    self.dump_cube(gf_occ.naux-1-i, cubefile="hoqmo%d.cube" % i)
                if (gf_vir.naux+i) < solver.gf.naux:
                    self.dump_cube(gf_vir.naux+i, cubefile="luqmo%d.cube" % i)

        solver.print_energies(output=True)

        self.log.info("Time elapsed:  %s", time_string(timer() - t0))

        return self.results


    def run(self):
        ''' Run self.kernel and return self

        Returns
        -------
        self: EAGF2
            `EAGF2` object containing calculation results.
        '''

        self.kernel()

        return self


    def print_clusters(self):
        """Print fragments of calculations."""
        self.log.info("%3s  %20s  %8s  %4s", "ID", "Name", "Solver", "Size")
        for frag in self.loop():
            self.log.info("%3d  %20s  %8s  %4d", frag.id, frag.name, frag.solver, frag.size)


    def __repr__(self):
        keys = ['mf']
        fmt = ('%s(' + len(keys)*'%s: %r, ')[:-2] + ')'
        values = [self.__dict__[k] for k in keys]
        return fmt % (self.__class__.__name__, *[x for y in zip(keys, values) for x in y])


if __name__ == '__main__':
    from pyscf import gto, scf

    mol = gto.Mole()
    mol.atom = ';'.join(['H 0 0 %d' % x for x in range(10)])
    mol.basis = 'sto6g'
    mol.verbose = 0
    mol.build()

    mf = scf.RHF(mol)
    mf = mf.density_fit(auxbasis='aug-cc-pvqz-ri')
    mf.conv_tol = 1e-12
    mf.kernel()
    assert mf.converged

    opts = {
        'conv_tol': 1e-8,
        'conv_tol_rdm1': 1e-12,
        'conv_tol_nelec': 1e-10,
        'conv_tol_nelec_factor': 1e-4,
    }

    eagf2 = EAGF2(mf, fragment_type='Lowdin-AO', max_bath_order=20)
    for i in range(mol.natm//2):
        eagf2.make_atom_fragment([i*2, i*2+1])
    eagf2.kernel()
    assert eagf2.converged

    from vayesta import log
    log.info("Full AGF2:")
    log.setLevel(25)
    gf2 = RAGF2(mf, log=log)
    gf2.kernel()
    assert gf2.converged