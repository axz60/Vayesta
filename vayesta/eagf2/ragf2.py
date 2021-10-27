# Standard library
import dataclasses
import logging
import copy
import sys

# NumPy
import numpy as np
import scipy.linalg

# PySCF
from pyscf import lib, agf2, ao2mo
from pyscf.scf import _vhf
from pyscf.agf2 import mpi_helper, _agf2
from pyscf.agf2 import chkfile as chkutil

# Vayesta
import vayesta
from vayesta.core.util import time_string, OptionsBase, NotSet
from vayesta.core import foldscf
from vayesta.eagf2 import helper

# Timings
if mpi_helper.mpi == None:
    from timeit import default_timer as timer
else:
    timer = mpi_helper.mpi.Wtime


@dataclasses.dataclass
class RAGF2Options(OptionsBase):
    ''' Options for RAGF2 calculations.
    '''

    # --- Auxiliary parameters
    weight_tol: float = 1e-12           # tolerance in weight of auxiliaries
    non_dyson: bool = False             # set to True for non-Dyson approximation
    nmom_lanczos: int = 0               # number of moments for block Lanczos
    nmom_projection: int = None         # number of moments for EwDMET projection
    os_factor: float = 1.0              # opposite-spin scaling factor
    ss_factor: float = 1.0              # same-spin scaling factor
    diagonal_se: bool = False           # use a diagonal approximation

    # --- Main convergence parameters
    max_cycle: int = 50                 # maximum number of AGF2 iterations
    conv_tol: float = 1e-7              # convergence tolerance in AGF2 energy
    conv_tol_t0: float = np.inf         # convergence tolerance in zeroth SE moment
    conv_tol_t1: float = np.inf         # convergence tolerance in first SE moment
    damping: float = 0.0                # damping of AGF2 iterations
    diis_space: int = 6                 # size of AGF2 DIIS space
    diis_min_space: int = 1             # minimum AGF2 DIIS space before extrapolation
    as_adc: bool = False                # convert to ADC(2) solver, see kernel_adc
    extra_cycle: bool = True            # if True, ensure convergence with extra cycle

    # --- Fock loop convergence parameters
    fock_basis: str = 'MO'              # basis to perform Fock build in
    fock_loop: bool = True              # do the Fock loop
    max_cycle_outer: int = 20           # maximum number of outer Fock loop cycles
    max_cycle_inner: int = 50           # maximum number of inner Fock loop cycles
    conv_tol_rdm1: float = 1e-8         # convergence tolerance in 1RDM
    conv_tol_nelec: float = 1e-6        # convergence tolerance in number of electrons
    conv_tol_nelec_factor: float = 1e-2 # control non-commutative convergence critera
    fock_diis_space: int = 8            # size of Fock loop DIIS space
    fock_diis_min_space: int = 1        # minimum Fock loop DIIS space before extrapolation
    fock_damping: float = 0.0           # damping of Fock matrices

    # --- Analysis
    pop_analysis: bool = False          # perform population analysis
    dip_moment: bool = False            # calculate dipole moments
    excitation_tol: float = 0.1         # tolerance for printing MO character of excitations
    excitation_number: int = 5          # number of excitations to print
                                        
    # --- Output                        
    dump_chkfile: bool = True           # dump results to chkfile
    chkfile: str = None                 # name of chkfile, if None then inherit from self.mf
    dump_cubefiles: int = 0             # number of HOQMOs and LUQMOs to dump to .cube files


class DIIS(lib.diis.DIIS):
    def __init__(self, **kwargs):
        fojb = lambda: None
        fojb.verbose = 0
        fojb.stdout = sys.stdout

        lib.diis.DIIS.__init__(self, fojb)

        self.__dict__.update(kwargs)

    def update(self, x, *args, **kwargs):
        try:
            return lib.diis.DIIS.update(self, x, *args, **kwargs)
        except np.linalg.linalg.LinAlgError:
            return x


def _active_slices(nmo, frozen):
    ''' Get slices for frozen occupied, active, frozen virtual spaces
    '''

    focc = slice(None, frozen[0])
    fvir = slice(nmo-frozen[1], None)
    act = slice(frozen[0], nmo-frozen[1])

    return focc, fvir, act


def _ao2mo_3c(eri, ci, cj, mpi=True):
    ''' AO2MO for three-centre integrals
    '''

    naux = eri.shape[0]
    ni, nj = ci.shape[1], cj.shape[1]
    cij = np.hstack((ci, cj))
    sij = (0, ci.shape[1], ci.shape[1], ci.shape[1]+cj.shape[1])
    sym = dict(aosym='s1', mosym='s1')

    dtype = np.result_type(eri.dtype, ci.dtype, cj.dtype)
    qij = np.zeros((naux, ni*nj), dtype=dtype)

    prange = mpi_helper.prange if mpi else lib.prange

    for p0, p1 in prange(0, naux, naux):
        eri0 = eri[p0:p1]
        if dtype is np.complex128:
            cij = np.asarray(cij, dtype=np.complex128)
            qij[p0:p1] = ao2mo._ao2mo.r_e2(eri0, cij, sij, [], None, out=qij[p0:p1])
        else:
            qij[p0:p1] = ao2mo._ao2mo.nr_e2(eri0, cij, sij, out=qij[p0:p1], **sym)

    if mpi:
        mpi_helper.barrier()
        mpi_helper.allreduce_safe_inplace(qij)

    return qij.reshape(naux, ni, nj)


def _ao2mo_4c(eri, ci, cj, ck, cl, mpi=True):
    ''' AO2MO for four-centre integrals
    '''

    nao = eri.shape[0]
    ni = ci.shape[1] if ci is not None else nao

    dtype = np.result_type(*(x.dtype for x in (eri, cj, ck, cl)))
    ijkl = np.zeros((ni, cj.shape[1], ck.shape[1], cl.shape[1]))

    prange = mpi_helper.prange if mpi else lib.prange

    for p0, p1 in prange(0, nao, nao):
        tmp = lib.einsum('pqrs,qj,rk,sl->pjkl', eri[p0:p1], cj, ck.conj(), cl)

        if ci is not None:
            ijkl += lib.einsum('pjkl,pi->ijkl', tmp, ci[p0:p1].conj())
        else:
            ijkl[p0:p1] = tmp

    if mpi:
        mpi_helper.barrier()
        mpi_helper.allreduce_safe_inplace(ijkl)

    return ijkl


def second_order_singles(agf2, gf=None, eri=None):
    ''' Compute the second-order correction to the singles part.
    '''

    if gf is None:
        gf = agf2.gf
    if eri is None:
        eri = agf2.eri

    gf_occ = gf.get_occupied()
    gf_vir = gf.get_virtual()
    e_occ, c_occ = gf_occ.energy, gf_occ.coupling
    e_vir, c_vir = gf_vir.energy, gf_vir.coupling

    if agf2.opts.non_dyson:
        e_full_occ, c_full_occ = e_occ, c_occ
        e_full_vir, c_full_vir = e_vir, c_vir
    else:
        e_full_occ, c_full_occ = gf.energy, gf.coupling
        e_full_vir, c_full_vir = gf.energy, gf.coupling

    h_2h1p = 0.0
    h_1h2p = 0.0

    if eri.ndim == 4:
        Δ = 1.0 / lib.direct_sum('x-i+a-j->xiaj', e_full_vir, e_occ, e_vir, e_occ)
        v = _ao2mo_4c(eri, c_full_vir, c_occ, c_vir, c_occ)

        h_2h1p  = lib.einsum('xiaj,yiaj,xiaj->xy', v, v, Δ)
        h_2h1p -= lib.einsum('xiaj,yjai,xiaj->xy', v, v, Δ) * 0.5
        h_2h1p  = lib.hermi_sum(h_2h1p)

        del v, Δ

        v = _ao2mo_4c(eri, c_full_occ, c_vir, c_occ, c_vir)
        Δ = 1.0 / lib.direct_sum('x-a+i-b->xaib', e_full_occ, e_vir, e_occ, e_vir)

        h_1h2p  = lib.einsum('xaib,yaib,xaib->xy', v, v, Δ)
        h_1h2p -= lib.einsum('xaib,ybia,xaib->xy', v, v, Δ) * 0.5
        h_1h2p  = lib.hermi_sum(h_1h2p)

        del v, Δ

    elif eri.ndim == 3:
        Lxi = _ao2mo_3c(eri, c_full_vir, c_occ)
        Laj = _ao2mo_3c(eri, c_vir, c_occ)
        for i in range(gf_occ.naux):
            v1 = lib.einsum('Lx,Laj->xaj', Lxi[:, :, i], Laj)
            v2 = lib.einsum('Lxi,La->xia', Lxi, Laj[:, : ,i])
            Δ = 1.0 / (lib.direct_sum('x+a-j->xaj', e_full_vir, e_vir, e_occ) - e_occ[i])

            h_2h1p_part  = lib.einsum('xaj,yaj,xaj->xy', v1, v1, Δ)
            h_2h1p_part -= lib.einsum('xaj,yja,xaj->xy', v1, v2, Δ) * 0.5
            h_2h1p += lib.hermi_sum(h_2h1p_part)

            del v1, v2, Δ

        Lxa = _ao2mo_3c(eri, c_full_occ, c_vir)
        Lib = _ao2mo_3c(eri, c_occ, c_vir)
        for a in range(gf_vir.naux):
            v1 = lib.einsum('Lx,Lib->xib', Lxa[:, :, a], Lib)
            v2 = lib.einsum('Lxa,Li->xai', Lxa, Lib[:, :, a])
            Δ = 1.0 / (lib.direct_sum('x+i-b->xib', e_full_occ, e_occ, e_vir) - e_vir[a])

            h_1h2p_part  = lib.einsum('xib,yib,xib->xy', v1, v1, Δ)
            h_1h2p_part -= lib.einsum('xib,ybi,xib->xy', v1, v2, Δ) * 0.5
            h_1h2p += lib.hermi_sum(h_1h2p_part)

            del v1, v2, Δ

    if agf2.opts.non_dyson:
        h = scipy.linalg.block_diag(h_1h2p, h_2h1p)
    else:
        h = h_2h1p + h_1h2p

    h = np.linalg.multi_dot((gf.coupling.T.conj(), h, gf.coupling))

    return h


class RAGF2:

    Options = RAGF2Options
    DIIS = DIIS

    def __init__(self, mf, log=None, options=None, frozen=(0, 0), eri=None,
                 mo_energy=None, mo_coeff=None, mo_occ=None, veff=None, rsjk=None, **kwargs):
        ''' 
        Restricted auxiliary second-order Green's function perturbation theory
        '''

        # Logging:
        self.log = log or vayesta.log
        self.log.info("Initializing " + self.__class__.__name__)
        self.log.info("*************" + "*" * len(str(self.__class__.__name__)))

        # Options:
        if options is None:
            self.opts = self.Options(**kwargs)
        else:
            self.opts = options.replace(kwargs)
        self.log.info(self.__class__.__name__ + " parameters:")
        for key, val in self.opts.items():
            self.log.info("  > %-28s %r", key + ":", val)

        # Set attributes:
        self.mol = mf.mol
        self.mf = mf
        self.mo_energy = mo_energy if mo_energy is not None else mf.mo_energy
        self.mo_coeff = mo_coeff if mo_coeff is not None else mf.mo_coeff
        self.mo_occ = mo_occ if mo_occ is not None else mf.get_occ(self.mo_energy, self.mo_coeff)
        self.h1e = self._get_h1e(self.mo_coeff)
        self.eri = eri
        self.rsjk = rsjk
        self.gf = self.se = None
        self.e_init = 0.0
        self.e_1b = self.mf.e_tot
        self.e_2b = 0.0
        self.veff = veff
        self._nmo = None   #FIXME
        self._nocc = None  #FIXME
        self.converged = False
        self.chkfile = self.opts.chkfile or self.mf.chkfile

        # Frozen masks:
        self.frozen = frozen
        self.focc, self.fvir, self.act = self._active_slices(self.nmo, self.frozen)

        # Print system information:
        self._print_sizes()

        # ERIs:
        if self.eri is not None:
            if self.eri.ndim == 3:
                self.log.info("ERIs passed by kwarg and will be density fitted")
            else:
                self.log.info("ERIs passed by kwarg and will be four centered")
        else:
            self.ao2mo()

        # Veff due to frozen density:
        self._get_frozen_veff(self.mo_coeff)


    def _active_slices(self, nmo, frozen):
        ''' Get slices for frozen occupied, active, frozen virtual spaces
        '''

        return _active_slices(nmo, frozen)


    def _get_h1e(self, mo_coeff):
        ''' Get the core Hamiltonian
        '''

        h1e_ao = self.mf.get_hcore()
        h1e = np.linalg.multi_dot((mo_coeff.T.conj(), h1e_ao, mo_coeff))

        return h1e


    def _get_frozen_veff(self, mo_coeff):
        ''' Get Veff due to the frozen density
        '''

        if self.veff is not None:
            self.log.info("Veff due to frozen density passed by kwarg")
        elif not all([x == (0, 0) for x in self.frozen]):
            self.log.info("Calculating Veff due to frozen density")
            c_focc = mo_coeff[:, self.focc]
            dm_froz = np.dot(c_focc, c_focc.T.conj()) * 2  #TODO is this correct? or project full RDM into frozen space? or are they the same?
            self.veff = self.mf.get_veff(dm=dm_froz)
            self.veff = np.linalg.multi_dot((mo_coeff.T.conj(), self.veff, mo_coeff))

        return self.veff


    def _print_sizes(self):
        ''' Print the system sizes
        '''

        nmo, nocc, nvir = self.nmo, self.nocc, self.nvir
        frozen, nact, nfroz = self.frozen, self.nact, self.nfroz

        self.log.info("               %6s %6s %6s", 'active', 'frozen', 'total')
        self.log.info("Occupied MOs:  %6d %6d %6d", nocc-frozen[0], frozen[0], nocc)
        self.log.info("Virtual MOs:   %6d %6d %6d", nvir-frozen[1], frozen[1], nvir)
        self.log.info("General MOs:   %6d %6d %6d", nact,           nfroz,     nmo)


    def _build_moments(self, eo, ev, xija, os_factor=None, ss_factor=None):
        ''' Build the occupied or virtual self-energy moments
        '''

        os_factor = os_factor or self.opts.os_factor
        ss_factor = ss_factor or self.opts.ss_factor
        facs = {'os_factor': os_factor, 'ss_factor': ss_factor}

        if self.opts.nmom_lanczos == 0 and not self.opts.diagonal_se:
            if isinstance(xija, tuple):
                t = _agf2.build_mats_dfragf2_incore(*xija, eo, ev, **facs)
            else:
                t = _agf2.build_mats_ragf2_incore(xija, eo, ev, **facs)
        else:
            nphys = xija[0].shape[1] if isinstance(xija, tuple) else xija.shape[0]
            dtype = xija[0].dtype

            t = np.zeros((2*self.opts.nmom_lanczos+2, nphys, nphys), dtype=dtype)
            ija = lib.direct_sum('i+j-a->ija', eo, eo, ev)

            fpos = os_factor + ss_factor
            fneg = -ss_factor

            for i in mpi_helper.nrange(eo.size):
                if isinstance(xija, tuple):
                    Qxi, Qja = xija
                    xja = lib.einsum('Qx,Qja->xja', Qxi[:,:,i], Qja).reshape(nphys, -1)
                    xia = lib.einsum('Qxi,Qa->xia', Qxi, Qja[:,i]).reshape(nphys, -1)
                else:
                    xja = xija[:,i].reshape(nphys, -1)
                    xia = xija[:,:,i].reshape(nphys, -1)

                for n in range(2*self.opts.nmom_lanczos+2):
                    xja_n = xja * np.ravel(ija[i]**n)[None]
                    if not self.opts.diagonal_se:
                        t[n] += (
                            + fpos * np.dot(xja_n, xja.T.conj())
                            + fneg * np.dot(xja_n, xia.T.conj())
                        )
                    else:
                        t[n][np.diag_indices_from(t[n])] += (
                            + fpos * np.sum(xja_n * xja.conj(), axis=1)
                            + fneg * np.sum(xja_n * xia.conj(), axis=1)
                        )

            mpi_helper.barrier()
            mpi_helper.allreduce_safe_inplace(t)

        return t


    def _build_se_from_moments(self, t, chempot=0.0, eps=1e-16):
        ''' Build the occupied or virtual self-energy from its moments
        '''

        nphys = t[0].shape[0]
        
        if self.opts.nmom_lanczos == 0:
            w, v = np.linalg.eigh(t[0])
            mask = w > eps
            w, v = w[mask], v[:, mask]
            b = np.dot(v * w[None]**0.5, v.T.conj())
            b_inv = np.dot(v * w[None]**-0.5, v.T.conj())
            e, v = np.linalg.eigh(np.linalg.multi_dot((b_inv.T.conj(), t[1], b_inv)))
            v = np.dot(b.T.conj(), v[:nphys])

        else:
            m, b = helper.block_lanczos(t)
            h_tri = helper.block_tridiagonal(m, b)
            e, v = np.linalg.eigh(h_tri[nphys:, nphys:])
            v = np.dot(b[0].T.conj(), v[:nphys])

        se = agf2.SelfEnergy(e, v, chempot=chempot)
        se.remove_uncoupled(tol=self.opts.weight_tol)

        return se


    def _combine_se(self, se_occ, se_vir, gf=None):
        ''' Combine the occupied and virtual self-energies
        '''

        se = agf2.aux.combine(se_occ, se_vir)

        if self.opts.nmom_projection is not None:
            gf = gf or self.gf
            fock = self.get_fock(gf=gf, with_frozen=False)
            se = se.compress(n=(self.opts.nmom_projection, None), phys=fock)

        se.remove_uncoupled(tol=self.opts.weight_tol)

        return se


    def ao2mo(self):
        ''' Get the ERIs in MO basis
        '''

        t0 = timer()

        if getattr(self.mf, 'with_df', None):
            self.log.info("ERIs will be density fitted")
            if self.mf.with_df._cderi is None:
                self.mf.with_df.build()
            if not isinstance(self.mf.with_df._cderi, np.ndarray):
                raise ValueError("DF _cderi object is not an array (%s)" % self.mf.with_df._cderi)
            mo_coeff = self.mo_coeff[:, self.act]
            self.eri = np.asarray(lib.unpack_tril(self.mf.with_df._cderi, axis=-1))
            self.eri = _ao2mo_3c(self.eri, mo_coeff, mo_coeff)
        else:
            self.log.info("ERIs will be four-centered")
            mo_coeff = self.mo_coeff[:, self.act]
            self.eri = ao2mo.incore.full(self.mf._eri, mo_coeff, compact=False)
            self.eri = self.eri.reshape((self.nact,) * 4)

        self.log.timing("Time for AO->MO:  %s", time_string(timer() - t0))

        return self.eri


    def build_self_energy(self, gf, se_prev=None):
        ''' Build the self-energy using a given Green's function
        '''

        t0 = timer()
        self.log.info("Building the self-energy")
        self.log.info("************************")

        qmo_energy = gf.energy
        qmo_coeff = gf.coupling
        qmo_occ = (gf.energy < gf.chempot).astype(int) * 2

        cx = np.eye(self.nact)
        ci = qmo_coeff[:, qmo_occ > 0]
        ca = qmo_coeff[:, qmo_occ == 0]

        ei = qmo_energy[qmo_occ > 0]
        ea = qmo_energy[qmo_occ == 0]

        if self.opts.non_dyson:
            xo = slice(None, self.nocc-self.frozen[0])
            xv = slice(self.nocc-self.frozen[0], None)
        else:
            xo = xv = slice(None)

        if self.eri.ndim == 3:
            qxi = _ao2mo_3c(self.eri, cx[:, xo], ci)
            qja = _ao2mo_3c(self.eri, ci, ca)
            qxa = _ao2mo_3c(self.eri, cx[:, xv], ca)
            qbi = qja.swapaxes(1, 2)
            xija = (qxi, qja)
            xabi = (qxa, qbi)
            dtype = qxi.dtype
        else:
            xija = _ao2mo_4c(self.eri[xo], None, ci, ci, ca)
            xabi = _ao2mo_4c(self.eri[xv], None, ca, ca, ci)
            dtype = xija.dtype

        self.log.timing("Time for MO->QMO:  %s", time_string(timer() - t0))
        t0 = timer()

        t_occ = np.zeros((2*self.opts.nmom_lanczos+2, self.nact, self.nact), dtype=dtype)
        t_vir = np.zeros((2*self.opts.nmom_lanczos+2, self.nact, self.nact), dtype=dtype)

        t_occ[:, xo, xo] = self._build_moments(ei, ea, xija)
        t_vir[:, xv, xv] = self._build_moments(ea, ei, xabi)

        del xija, xabi

        for i in range(2*self.opts.nmom_lanczos+2):
            self.log.debug(
                    "Trace of n=%d moments:  Occupied = %.5g  Virtual = %.5g",
                    i, np.trace(t_occ[i]), np.trace(t_vir[i]),
            )


        # === Occupied:

        self.log.info("Occupied self-energy:")
        self.log.changeIndentLevel(1)
        self.log.debug("Number of ija:  %s", ei.size**2 * ea.size)

        w = np.linalg.eigvalsh(t_occ[0])
        wmin, wmax = w.min(), w.max()
        (self.log.warning if wmin < 1e-8 else self.log.debug)(
                'Eigenvalue range:  %.5g -> %.5g', wmin, wmax,
        )

        se_occ = self._build_se_from_moments(t_occ, chempot=gf.chempot)

        self.log.info("Built %d occupied auxiliaries", se_occ.naux)
        self.log.changeIndentLevel(-1)


        # === Virtual:
        
        self.log.info("Virtual self-energy:")
        self.log.changeIndentLevel(1)
        self.log.debug("Number of abi:  %s", ei.size * ea.size**2)

        w = np.linalg.eigvalsh(t_vir[0])
        wmin, wmax = w.min(), w.max()
        (self.log.warning if wmin < 1e-8 else self.log.debug)(
                'Eigenvalue range:  %.5g -> %.5g', wmin, wmax,
        )

        se_vir = self._build_se_from_moments(t_vir, chempot=gf.chempot)

        self.log.info("Built %d virtual auxiliaries", se_vir.naux)
        self.log.changeIndentLevel(-1)

        nh = self.nocc-self.frozen[0]
        wt = lambda v: np.sum(v * v)
        self.log.infov("Total weights of coupling blocks:")
        self.log.infov("        %6s  %6s", "2h1p", "1h2p")
        self.log.infov("    1h  %6.4f  %6.4f", wt(se_occ.coupling[:nh]), wt(se_occ.coupling[nh:]))
        self.log.infov("    1p  %6.4f  %6.4f", wt(se_vir.coupling[:nh]), wt(se_vir.coupling[nh:]))


        se = self._combine_se(se_occ, se_vir, gf=gf)

        self.log.info("Number of auxiliaries built:  %s", se.naux)
        self.log.timing("Time for self-energy build:  %s", time_string(timer() - t0))

        return se


    def run_diis(self, se, gf, diis, se_prev=None):
        ''' Update the self-energy using DIIS and apply damping
        '''

        t = np.array((
            se.get_occupied().moment(range(2*self.opts.nmom_lanczos+2)),
            se.get_virtual().moment(range(2*self.opts.nmom_lanczos+2)),
        ))
        self.log.debug("Summed trace of moments:")
        self.log.debug(" > Initial :  %.5g", np.einsum('onii->', t))

        if self.opts.damping and se_prev:
            t_prev = np.array((
                se_prev.get_occupied().moment(range(2*self.opts.nmom_lanczos+2)),
                se_prev.get_virtual().moment(range(2*self.opts.nmom_lanczos+2)),
            ))

            t *= (1.0 - self.opts.damping)
            t += self.opts.damping * t_prev
            self.log.debug(" > Damping :  %.5g", np.einsum('onii->', t))

        t = diis.update(t)
        self.log.debug(" > DIIS    :  %.5g", np.einsum('onii->', t))

        se_occ = self._build_se_from_moments(t[0], chempot=se.chempot)
        se_vir = self._build_se_from_moments(t[1], chempot=se.chempot)

        se = self._combine_se(se_occ, se_vir, gf=gf)

        return se


    def build_init_greens_function(self):
        ''' Build the mean-field Green's function
        '''

        chempot = 0.5 * (
                + self.mo_energy[self.mo_occ > 0].max()
                + self.mo_energy[self.mo_occ == 0].min()
        )

        e = self.mo_energy[self.act]
        v = np.eye(self.nact)
        gf = agf2.GreensFunction(e, v, chempot=chempot)

        self.log.debug("Built G0 with μ(MF) = %.5g", chempot)
        self.log.info("Number of active electrons in G0:  %s", np.trace(gf.make_rdm1()))

        return gf


    def solve_dyson(self, se=None, gf=None, fock=None):
        ''' Solve the Dyson equation
        '''

        se = se or self.se
        gf = gf or self.gf

        if fock is None:
            fock = self.get_fock(gf=gf, with_frozen=False)

        e = se.energy
        v = se.coupling

        f_ext = np.block([[fock, v], [v.T.conj(), np.diag(e)]])
        w, v = np.linalg.eigh(f_ext)

        self.log.debugv("Solved Dyson equation, eigenvalue ranges:")
        self.log.debugv(
                " > Occupied :  %.5g -> %.5g",
                np.min(w[w < se.chempot]),
                np.max(w[w < se.chempot]),
        )
        self.log.debugv(
                " > Virtual  :  %.5g -> %.5g",
                np.min(w[w >= se.chempot]),
                np.max(w[w >= se.chempot]),
        )

        return w, v


    def fock_loop(self, gf=None, se=None, fock=None, project_gf=True, return_fock=False):
        ''' Do the self-consistent Fock loop
        '''

        t0 = timer()
        gf = gf or self.gf
        se = se or self.se

        nelec  = (self.nocc - self.frozen[0]) * 2  #FIXME yes?
        if fock is None:
            fock = self.get_fock(gf=gf, with_frozen=False)

        if not self.opts.fock_loop:
            # Just solve Dyson eqn
            self.log.info("Solving Dyson equation")
            w, v = self.solve_dyson(se=se, gf=gf, fock=fock)
            if project_gf:
                gf = agf2.GreensFunction(w, v[:self.nact], chempot=se.chempot)
            else:
                gf = agf2.GreensFunction(w, v, chempot=se.chempot)
            gf.chempot = se.chempot = agf2.chempot.binsearch_chempot((w, v), self.nact, nelec)[0]
            return gf, se, True

        self.log.info('Fock loop')
        self.log.info('*********')

        diis = self.DIIS(space=self.opts.fock_diis_space, min_space=self.opts.fock_diis_min_space)
        rdm1_prev = np.zeros_like(fock)
        converged = False

        self.log.debug("Target number of electrons:  %d", nelec)
        self.log.infov('%12s %9s %12s %12s', 'Iteration', 'Cycles', 'Nelec error', 'DM change')

        for niter1 in range(1, self.opts.max_cycle_outer+1):
            se, opt = agf2.chempot.minimize_chempot(
                    se, fock, nelec,
                    x0=se.chempot,
                    tol=self.opts.conv_tol_nelec*self.opts.conv_tol_nelec_factor,
                    maxiter=self.opts.max_cycle_inner,
            )

            for niter2 in range(1, self.opts.max_cycle_inner+1):
                w, v = self.solve_dyson(se=se, fock=fock)
                se.chempot, nerr = agf2.chempot.binsearch_chempot((w, v), self.nact, nelec)
                gf = agf2.GreensFunction(w, v[:self.nact], chempot=se.chempot)

                fock_prev = fock.copy()
                fock = self.get_fock(gf=gf, with_frozen=False)

                if self.opts.fock_damping:
                    fock *= (1.0 - self.opts.fock_damping)
                    fock += self.opts.fock_damping * fock_prev

                rdm1 = self.make_rdm1(gf=gf, with_frozen=False)
                fock = diis.update(fock, xerr=None)

                derr = np.max(np.absolute(rdm1 - rdm1_prev))
                rdm1_prev = rdm1.copy()

                self.log.debugv('%12s %9s %12.4g %12.4g', '(*) %d'%niter1, '-> %d'%niter2, nerr, derr)

                if abs(derr) < self.opts.conv_tol_rdm1:
                    break

            self.log.infov('%12d %9d %12.4g %12.4g', niter1, niter2, nerr, derr)

            if abs(derr) < self.opts.conv_tol_rdm1 and abs(nerr) < self.opts.conv_tol_nelec:
                converged = True
                break

        if not project_gf:
            gf = agf2.GreensFunction(w, v, chempot=se.chempot)

        (self.log.info if converged else self.log.warning)("Converged = %r", converged)
        self.log.info("μ = %.9g", se.chempot)
        self.log.timing('Time for fock loop:  %s', time_string(timer() - t0))

        if not return_fock:
            return gf, se, converged
        else:
            return gf, se, converged, fock


    def get_fock(self, gf=None, rdm1=None, with_frozen=True, fock_last=None):
        ''' Get the Fock matrix including all frozen contributions
        '''
        #TODO check these expressions for complex ERIs

        if self.opts.fock_basis.lower() == 'ao':
            return self._get_fock_via_ao(gf=gf, rdm1=rdm1, with_frozen=with_frozen)
        elif self.opts.fock_basis.lower() == 'adc':
            h2 = second_order_singles(self, gf=gf)
            return np.diag(self.mo_energy) + h2
        elif self.opts.fock_basis.lower() == 'rsjk':
            return self._get_fock_via_rsjk(
                    gf=gf, rdm1=rdm1, with_frozen=with_frozen, fock_last=fock_last)

        t0 = timer()
        self.log.debugv("Building Fock matrix")

        gf = gf or self.gf
        if rdm1 is None:
            rdm1 = self.make_rdm1(gf=gf, with_frozen=False)
        eri = self.eri

        vj = np.zeros((self.nact, self.nact))
        vk = np.zeros((self.nact, self.nact))

        if eri.ndim == 4:
            for i0, i1 in mpi_helper.prange(0, self.nmo, self.nmo):
                i = slice(i0, i1)
                vj[i] += lib.einsum('ijkl,kl->ij', eri[i], rdm1)
                vk[i] += lib.einsum('iklj,kl->ij', eri[i], rdm1)
        else:
            naux = eri.shape[0]
            for q0, q1 in mpi_helper.prange(0, naux, naux):
                q = slice(q0, q1)
                tmp = lib.einsum('Qik,kl->Qil', eri[q], rdm1)
                vj += lib.einsum('Qij,Qkk->ij', eri[q], tmp)
                vk += lib.einsum('Qlj,Qil->ij', eri[q], tmp)

        mpi_helper.barrier()
        mpi_helper.allreduce_safe_inplace(vj)
        mpi_helper.allreduce_safe_inplace(vk)

        fock = vj - 0.5 * vk
        if self.veff is not None:
            fock += self.veff[self.act, self.act]
        fock += self.h1e[self.act, self.act]

        if with_frozen:
            fock_ref = np.diag(self.mo_energy)
            fock_ref[self.act, self.act] = fock
            fock = fock_ref

        self.log.timingv("Time for Fock matrix:  %s", time_string(timer() - t0))
        
        return fock


    def _get_fock_via_ao(self, gf=None, rdm1=None, with_frozen=True):
        '''
        Get the Fock matrix via AO basis integrals - result is still
        transformed into MO basis.
        '''
        #TODO Δdm algorithm for integral-direct

        t0 = timer()
        self.log.debugv("Building Fock matrix via AO integrals")

        gf = gf or self.gf
        mo_coeff = self.mo_coeff
        if rdm1 is None:
            rdm1 = self.make_rdm1(gf=gf, with_frozen=True)
        rdm1_ao = np.linalg.multi_dot((mo_coeff, rdm1, mo_coeff.T.conj()))

        veff_ao = self.mf.get_veff(dm=rdm1_ao)
        veff = np.linalg.multi_dot((mo_coeff.T.conj(), veff_ao, mo_coeff))

        fock = self.h1e + veff

        if not with_frozen:
            fock = fock[self.act, self.act]

        self.log.timingv("Time for Fock matrix:  %s", time_string(timer() - t0))
        
        return fock


    def _get_fock_via_rsjk(self, gf=None, rdm1=None, with_frozen=True, rdm1_last=None, fock_last=None):
        '''
        Get the Fock matrix via unfolding the Fock matrix calculated
        in the Born-van Karman supercell using a range-separation JK
        builder. Supports direct calculations.
        '''

        t0 = timer()
        self.log.debugv("Building Fock matrix via RSJK")

        gf = gf or self.gf
        mo_coeff = self.mo_coeff
        if rdm1 is None:
            rdm1 = self.make_rdm1(gf=gf, with_frozen=True)
        rdm1_ao = np.linalg.multi_dot((mo_coeff, rdm1, mo_coeff.T.conj()))

        if rdm1_last is not None:
            if rdm1_last.shape != rdm1.shape:
                # rdm1_last is frozen - pad with zeros
                rdm1_last_ = np.zeros_like(rdm1)
                rdm1_last_[self.act, self.act] = rdm1_last
                rdm1_last = rdm1_last_
            rdm1_last_ao = np.linalg.multi_dot((mo_coeff, rdm1_last, mo_coeff.T.conj()))
            veff_last = fock_last - self.h1e
        else:
            rdm1_last_ao = veff_last = 0

        rdm1_ao_kpts = foldscf.bvk2k_2d(rdm1_ao - rdm1_last_ao, self.rsjk.phase)
        vj_ao_kpts, vk_ao_kpts = self.rsjk.get_jk(rdm1_ao_kpts)
        veff_ao_kpts = vj_ao_kpts - 0.5 * vk_ao_kpts
        veff_ao = foldscf.k2bvk_2d(veff_ao_kpts, self.rsjk.phase)

        veff = np.linalg.multi_dot((mo_coeff.T.conj(), veff_ao, mo_coeff))
        veff += veff_last

        fock = self.h1e + veff

        if not with_frozen:
            fock = fock[self.act, self.act]

        self.log.timingv("Time for Fock matrix:  %s", time_string(timer() - t0))

        return fock
        

    def make_rdm1(self, gf=None, with_frozen=True):
        ''' Get the 1RDM
        '''

        gf = gf or self.gf
        rdm1 = gf.make_rdm1()

        if with_frozen:
            sc = np.dot(self.mf.get_ovlp(), self.mo_coeff)
            rdm1_ref = self.mf.make_rdm1(self.mo_coeff, self.mo_occ)
            rdm1_ref = np.linalg.multi_dot((sc.T, rdm1_ref, sc))
            rdm1_ref[self.act, self.act] = rdm1
            rdm1 = rdm1_ref

        return rdm1


    def make_rdm2(self, gf=None, with_frozen=True):
        ''' Get the 2RDM

            NOTE: this is experimental and is not numerically exact due
            to the compressed scheme.
        '''

        gf = gf or self.gf
        eri = self.eri

        if eri.ndim == 3:
            self.log.warning("make_rdm2 does not support DF, building 4c tensor")
            eri = lib.einsum('Qij,Qkl->ijkl', eri, eri)

        gf_occ, gf_vir = gf.get_occupied(), gf.get_virtual()
        ei, ci = gf_occ.energy, gf_occ.coupling
        ea, ca = gf_vir.energy, gf_vir.coupling

        iajb = _ao2mo_4c(self.eri, ci, ca, ci, ca)
        e_ia = lib.direct_sum('i-a->ia', ei, ea)
        t2 = iajb
        t2 /= lib.direct_sum('ia,jb->iajb', e_ia, e_ia)
        rdm2 = _ao2mo_4c(t2, ci.T.conj(), ca.T.conj(), ci.T.conj(), ca.T.conj())

        return rdm2


    def energy_mp2(self, mo_energy=None, se=None, flip=False):
        ''' Calculate the MP2 energy
        '''

        mo_energy = mo_energy if mo_energy is not None else self.mo_energy
        se = se or self.se

        if not flip:
            mo = mo_energy < se.chempot
            se = se.get_virtual()
        else:
            mo = mo_energy >= se.chempot
            se = se.get_occupied()

        v_se = se.coupling[mo]
        Δ = lib.direct_sum('x,k->xk', mo_energy[mo], -se.energy)

        e_mp2 = np.sum(v_se * v_se.conj() / Δ)
        e_mp2 = e_mp2.real

        return e_mp2


    def energy_1body(self, gf=None, e_nuc=None):
        ''' Calculate the one-body energy
        '''

        rdm1 = self.make_rdm1(gf=gf, with_frozen=True)
        fock = self.get_fock(gf=gf, with_frozen=True)
        h1e = self.h1e

        e1b  = 0.5 * np.sum(rdm1 * (h1e + fock))
        e1b += e_nuc if e_nuc is not None else self.e_nuc

        return e1b


    def energy_2body(self, gf=None, se=None, flip=False):
        ''' Calculate the two-body energy
        '''

        gf = gf or self.gf
        se = se or self.se

        if not flip:
            gf = gf.get_occupied()
            se = se.get_virtual()
        else:
            gf = gf.get_virtual()
            se = se.get_occupied()

        e_2b = 0.0
        for i in range(gf.naux):
            v_gf = gf.coupling[:, i]
            v_se = se.coupling
            v_dyson = v_se * v_gf[:, None]
            Δ = gf.energy[i] - se.energy

            e_2b += np.sum(np.dot(v_dyson / Δ[None], v_dyson.T.conj())).real

        e_2b *= 2.0

        return e_2b


    def population_analysis(self, method='meta-lowdin', pre_orth_method='ANO'):
        ''' Population analysis
        '''

        from pyscf.lo import orth
        from pyscf.scf.hf import mulliken_pop

        s = self.mol.get_ovlp()
        orth_coeff = orth.orth_ao(self.mol, method, pre_orth_method, s=s)
        c_inv = np.dot(orth_coeff.T.conj(), s)

        def mulliken(dm):
            dm = np.linalg.multi_dot((c_inv, dm, c_inv.T.conj()))
            return mulliken_pop(self.mol, dm, np.eye(orth_coeff.shape[0]), verbose=0)

        rdm1_hf = self.mf.make_rdm1(self.mo_coeff, self.mo_occ)
        rdm1_agf2 = np.linalg.multi_dot((self.mo_coeff, self.make_rdm1(), self.mo_coeff.T.conj()))

        pop_mo, charges_mo = mulliken(rdm1_hf)
        pop_qmo, charges_qmo = mulliken(rdm1_agf2)

        self.log.info("Population analysis")
        self.log.info("*******************")
        self.log.changeIndentLevel(1)
        self.log.info("%4s  %-12s %12s %12s", "AO", "Label", "Pop. (MF)", "Pop. (AGF2)")
        for i, s in enumerate(self.mol.ao_labels()):
            self.log.info("%4d  %-12s %12.6f %12.6f", i, s, pop_mo[i], pop_qmo[i])
        self.log.changeIndentLevel(-1)

        self.log.info("Atomic charges")
        self.log.info("**************")
        self.log.changeIndentLevel(1)
        self.log.info("%4s  %-12s %12s %12s", "Atom", "Symbol", "Charge (MF)", "Charge (AGF2)")
        for i in range(self.mol.natm):
            s = self.mol.atom_symbol(i)
            self.log.info("%4d  %-12s %12.6f %12.6f", i, s, charges_mo[i], charges_qmo[i])
        self.log.changeIndentLevel(-1)

        return pop_qmo, charges_qmo


    def dip_moment(self):
        ''' Dipole moment
        '''

        from pyscf.scf.hf import dip_moment

        dm_hf = self.mf.make_rdm1()

        dm_agf2 = self.make_rdm1()
        dm_agf2 = np.linalg.multi_dot((self.mo_coeff, dm_agf2, self.mo_coeff.T.conj()))

        dip_mo = dip_moment(self.mol, dm_hf, unit='au', verbose=0)
        tot_mo = np.linalg.norm(dip_mo)

        dip_qmo = dip_moment(self.mol, dm_agf2, unit='au', verbose=0)
        tot_qmo = np.linalg.norm(dip_qmo)

        self.log.info("Dipole moment")
        self.log.info("*************")
        self.log.changeIndentLevel(1)
        self.log.info("%6s %12s %12s", "Part", "Dip. (MF)", "Dip. (AGF2)")
        for x in range(3):
            self.log.info("%6s %12.6f %12.6f", "XYZ"[x], dip_mo[x], dip_qmo[x])
        self.log.info("%6s %12.6f %12.6f", "Total", tot_mo, tot_qmo)
        self.log.changeIndentLevel(-1)

        return dip_qmo


    def dump_chk(self, chkfile=None):
        ''' Save the calculation state
        '''

        chkfile = chkfile or self.chkfile

        if chkfile is None:
            return self

        chkutil.dump_agf2(self, chkfile=chkfile)

        return self


    def update_from_chk(self, chkfile=None):
        ''' Update from the calculation state
        '''

        chkfile = chkfile or self.chkfile

        if self.chkfile is None:
            return self

        mol, data = chkutil.load_agf2(self.chkfile, key)
        self.__dict__.update(data)

        return self


    def dump_cube(self, index, cubefile='agf2.cube', ngrid=200):
        ''' Dump a QMO to a .cube file
        '''
        #TODO: test

        from pyscf.tools import cubegen

        qmo_coeff = self.qmo_coeff

        if isinstance(grid, int):
            grid = (grid, grid, grid)

        cubegen.orbital(self.mol, cubefile, qmo_coeff[:,i], *grid)

        return self


    def print_excitations(self, gf=None):
        ''' Print the excitations and some information on their character
        '''

        gf = gf or self.gf
        gf_occ, gf_vir = gf.get_occupied(), gf.get_virtual()

        self.log.info("Excitations")
        self.log.info("***********")
        self.log.changeIndentLevel(1)

        self.log.info("%2s %12s %12s %s", "IP", "Energy", "QP weight", " Dominant MOs")
        for n in range(min(gf_occ.naux, self.opts.excitation_number)):
            en = -gf_occ.energy[-n-1]
            vn = gf_occ.coupling[:, -n-1]
            qpwt = np.linalg.norm(vn)**2
            char_string = ""
            num = 0
            for i in np.argsort(vn**2)[::-1]:
                if vn[i]**2 > self.opts.excitation_tol:
                    if num == 3:
                        char_string += " ..."
                        break
                    char_string += "%3d (%7.3f %%) " % (i, np.abs(vn[i]**2)*100)
                    num += 1
            self.log.info("%2d %12.6f %12.6f  %s", n, en, qpwt, char_string)

        self.log.info("%2s %12s %12s %s", "EA", "Energy", "QP weight", " Dominant MOs")
        for n in range(min(gf_vir.naux, self.opts.excitation_number)):
            en = gf_vir.energy[n]
            vn = gf_vir.coupling[:, n]
            qpwt = np.linalg.norm(vn)**2
            char_string = ""
            num = 0
            for i in np.argsort(vn**2)[::-1]:
                if vn[i]**2 > self.opts.excitation_tol:
                    if num == 3:
                        char_string += " ..."
                        break
                    char_string += "%3d (%7.3f %%) " % (i, np.abs(vn[i]**2)*100)
                    num += 1
            self.log.info("%2d %12.6f %12.6f  %s", n, en, qpwt, char_string)

        self.log.changeIndentLevel(-1)


    def print_energies(self, output=False):
        ''' Print the energies
        '''

        self.log.info("Energies")
        self.log.info("********")
        self.log.changeIndentLevel(1)

        logger = self.log.output if output else self.log.info

        logger("E(corr) = %20.12f", self.e_corr)
        logger("E(1b)   = %20.12f", self.e_1b)
        logger("E(2b)   = %20.12f", self.e_2b)
        logger("E(tot)  = %20.12f", self.e_tot)

        logger("IP      = %20.12f", self.e_ip)
        logger("EA      = %20.12f", self.e_ea)
        logger("Gap     = %20.12f", self.e_ip + self.e_ea)

        self.log.changeIndentLevel(-1)


    def _convergence_checks(self, se=None, se_prev=None, e_prev=None):
        ''' 
        Return a list of [energy, 0th moment, 1st moment] changes between
        iterations to check convergence progress.
        '''

        se = se or self.se
        e_prev = e_prev or self.mf.e_tot

        t0, t1 = se.moment(range(2), squeeze=False)
        if se_prev is None:
            t0_prev = t1_prev = np.zeros_like(t0)
        else:
            t0_prev, t1_prev = se_prev.moment(range(2), squeeze=False)

        deltas = [
                np.abs(self.e_tot - e_prev),
                np.linalg.norm(t0 - t0_prev),
                np.linalg.norm(t1 - t1_prev),
        ]

        return deltas


    def kernel(self):
        ''' Driving function for RAGF2
        '''

        if self.opts.as_adc:
            return self.kernel_adc()

        t0 = timer()

        if self.gf is None:
            gf = self.gf = self.g0 = self.build_init_greens_function()
        else:
            gf = self.gf
            self.log.info("Initial GF was already initialised")

        if self.se is None:
            se = self.se = self.build_self_energy(gf)
        else:
            se = self.se
            self.log.info("Initial SE was already initialised")

        diis = self.DIIS(space=self.opts.diis_space, min_space=self.opts.diis_min_space)

        e_mp2 = self.e_init = self.energy_mp2(se=se)
        self.log.info("Initial energies")
        self.log.info("****************")
        self.log.info("E(nuc)  = %20.12f", self.e_nuc)
        self.log.info("E(MF)   = %20.12f", self.e_mf)
        self.log.info("E(corr) = %20.12f", e_mp2)
        self.log.info("E(tot)  = %20.12f", self.mf.e_tot + e_mp2)

        converged = self.converged = False
        converged_prev = False
        se_prev = None
        for niter in range(1, self.opts.max_cycle+1):
            t1 = timer()
            self.log.info("Iteration %d", niter)
            self.log.info("**********%s", "*" * len(str(niter)))
            self.log.changeIndentLevel(1)

            se_prev = copy.deepcopy(self.se)
            e_prev = self.e_tot

            # one-body terms
            gf, se, _ = self.gf, self.se, fconv = self.fock_loop(gf=gf, se=se)
            e_1b = self.e_1b = self.energy_1body(gf=gf)

            # two-body terms
            se = self.se = self.build_self_energy(gf=gf, se_prev=se_prev)
            se = self.se = self.run_diis(se, gf, diis, se_prev=se_prev)
            e_2b = self.e_2b = self.energy_2body(gf=gf, se=se)

            self.print_excitations()
            self.print_energies()

            deltas = self._convergence_checks(se=se, se_prev=se_prev, e_prev=e_prev)

            self.log.info("Change in energy:     %10.3g", deltas[0])
            self.log.info("Change in 0th moment: %10.3g", deltas[1])
            self.log.info("Change in 1st moment: %10.3g", deltas[2])

            if self.opts.dump_chkfile and self.chkfile is not None:
                self.log.debug("Dumping current iteration to chkfile")
                self.dump_chk()

            self.log.timing("Time for AGF2 iteration:  %s", time_string(timer() - t1))

            self.log.changeIndentLevel(-1)

            if deltas[0] < self.opts.conv_tol \
                    and deltas[1] < self.opts.conv_tol_t0 \
                    and deltas[2] < self.opts.conv_tol_t1:
                if self.opts.extra_cycle and not converged_prev:
                    converged_prev = True
                else:
                    converged = self.converged = True
                    break
            else:
                if self.opts.extra_cycle and converged_prev:
                    converged_prev = False

        (self.log.info if converged else self.log.warning)("Converged = %r", converged)

        if self.opts.pop_analysis:
            self.population_analysis()

        if self.opts.dip_moment:
            self.dip_moment()

        if self.opts.dump_cubefiles:
            #TODO test, generalise for periodic
            self.log.debug("Dumping orbitals to .cube files")
            gf_occ, gf_vir = self.gf.get_occupied(), self.gf.get_virtual()
            for i in range(self.opts.dump_cubefiles):
                if (gf_occ.naux-1-i) >= 0:
                    self.dump_cube(gf_occ.naux-1-i, cubefile="hoqmo%d.cube"%i)
                if (gf_occ.naux+i) < gf.naux:
                    self.dump_cube(gf_occ.naux+i, cubefile="luqmo%d.cube"%i)

        self.print_energies(output=True)

        self.log.info("Time elapsed:  %s", time_string(timer() - t0))

        return se, gf, converged


    def kernel_adc(self):
        ''' 
        Kernel for an ADC-like calculation.

        Only max_cycle = 0 is implicit, other options to convert the
        solver to ADC must be provided, i.e.:

            non_dyson = True
            nmom_lanczos = 10
            fock_basis = 'adc'
            fock_loop = False
        '''

        t0 = timer()

        if self.gf is None:
            gf = self.gf = self.g0 = self.build_init_greens_function()
        else:
            gf = self.gf
            self.log.info("Initial GF was already initialised")

        if self.se is None:
            se = self.se = self.build_self_energy(gf)
        else:
            se = self.se
            self.log.info("Initial SE was already initialised")

        e_mp2 = self.e_init = self.energy_mp2(se=se)
        self.log.info("Initial energies")
        self.log.info("****************")
        self.log.info("E(nuc)  = %20.12f", self.e_nuc)
        self.log.info("E(MF)   = %20.12f", self.e_mf)
        self.log.info("E(corr) = %20.12f", e_mp2)
        self.log.info("E(tot)  = %20.12f", self.mf.e_tot + e_mp2)

        gf, se, _ = self.gf, self.se, fconv = self.fock_loop(gf=gf, se=se)

        if self.opts.pop_analysis:
            self.population_analysis()

        if self.opts.dip_moment:
            self.dip_moment()

        if self.opts.dump_cubefiles:
            #TODO test, generalise for periodic
            self.log.debug("Dumping orbitals to .cube files")
            gf_occ, gf_vir = self.gf.get_occupied(), self.gf.get_virtual()
            for i in range(self.opts.dump_cubefiles):
                if (gf_occ.naux-1-i) >= 0:
                    self.dump_cube(gf_occ.naux-1-i, cubefile="hoqmo%d.cube"%i)
                if (gf_occ.naux+i) < gf.naux:
                    self.dump_cube(gf_occ.naux+i, cubefile="luqmo%d.cube"%i)

        self.print_energies(output=True)

        self.log.info("Time elapsed:  %s", time_string(timer() - t0))

        return se, gf, True


    def run(self):
        ''' Run self.kernel and return self
        '''

        self.kernel()

        return self


    @property
    def qmo_energy(self):
        return self.gf.energy

    @property
    def qmo_coeff(self):
        return np.dot(self.mo_coeff, self.gf.coupling)

    @property
    def qmo_occ(self):
        occ = np.linalg.norm(self.gf.get_occupied().coupling, axis=0)**2
        vir = np.zeros_like(self.gf.get_virtual().energy)
        return np.concatenate([occ, vir], axis=0)

    dyson_orbitals = qmo_coeff


    @property
    def frozen(self):
        return getattr(self, '_frozen', (0, 0))
    @frozen.setter
    def frozen(self, frozen):
        if frozen == None or frozen == 0 or frozen == (0, 0):
            self._frozen = (0, 0)
        elif isinstance(frozen, int):
            self._frozen = (frozen, 0)
        else:
            self._frozen = frozen

    @property
    def e_tot(self): return self.e_1b + self.e_2b
    @property
    def e_nuc(self): return self.mol.energy_nuc()
    @property
    def e_mf(self): return self.mf.e_tot
    @property
    def e_corr(self): return self.e_tot - self.e_mf

    @property
    def e_ip(self): return -self.gf.get_occupied().energy.max()
    @property
    def e_ea(self): return self.gf.get_virtual().energy.min()

    @property
    def nmo(self): return self._nmo or self.mo_energy.size
    @property
    def nocc(self): return self._nocc or np.sum(self.mo_occ > 0)
    @property
    def nvir(self): return self.nmo - self.nocc
    @property
    def nfroz(self): return self.frozen[0] + self.frozen[1]
    @property
    def nact(self): return self.nmo - self.nfroz



if __name__ == '__main__':
    from pyscf import gto, scf

    mol = gto.M(atom='O 0 0 0; O 0 0 1', basis='aug-cc-pvdz', verbose=0)
    #mol = gto.M(atom='O 0 0 0; H 0 0 1; H 0 1 0', basis='cc-pvdz', verbose=0)
    rhf = scf.RHF(mol).run()#density_fit().run()
    gf2 = RAGF2(
            rhf,
            frozen=(0,0),
            non_dyson=False,
            fock_loop=True,
            nmom_lanczos=0,
            nmom_projection=None,
            diagonal_se=False,
            #fock_basis='ao',
    )
    gf2.run()

    test = False

    if test:
        mols = [
            gto.M(atom='H 0 0 0; Li 0 0 1.64', basis='aug-cc-pvdz', verbose=0),
            gto.M(atom='O 0 0 0; H 0 0 1; H 0 1 0', basis='cc-pvdz', verbose=0),
            gto.M(atom='O 0 0 0; O 0 0 1', basis='6-31g', verbose=0),
        ]

        for mol in mols:
            rhf = scf.RHF(mol).run()
            gf2_a = RAGF2(rhf).run()
            gf2_b = agf2.RAGF2(rhf).run()
            
            assert np.allclose(gf2_a.e_tot, gf2_b.e_tot)
            assert np.allclose(gf2_a.gf.energy, gf2_b.gf.energy)

            rhf = scf.RHF(mol).density_fit().run()
            gf2_a = RAGF2(rhf).run()
            gf2_b = agf2.RAGF2(rhf).run()
            
            assert np.allclose(gf2_a.e_tot, gf2_b.e_tot)
            assert np.allclose(gf2_a.gf.energy, gf2_b.gf.energy)
