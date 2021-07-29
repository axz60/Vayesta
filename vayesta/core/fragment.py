import dataclasses

import numpy as np
import scipy
import scipy.linalg

import pyscf
import pyscf.lib
import pyscf.lo

from vayesta.core.util import *
from . import helper
from . import tsymmetry


class QEmbeddingFragment:


    @dataclasses.dataclass
    class Options(OptionsBase):
        sym_factor: float = 1.0
        coupled_fragments: list = dataclasses.field(default_factory=list)
        # Translationally symmetry
        tsym_parent: 'typing.Any' = None
        tsym_order: list = None
        tsym_inverse: list = None


    @dataclasses.dataclass
    class Results:
        fid: int = None


    class Exit(Exception):
        """Raise for controlled early exit."""
        pass


    def __init__(self, base, fid, name, c_frag, c_env, fragment_type, atoms=None, aos=None, log=None, options=None, **kwargs):
        """Abstract base class for quantum embedding fragments.

        The fragment may keep track of associated atoms or atomic orbitals, using
        the `atoms` and `aos` attributes, respectively.

        Parameters
        ----------
        base : QEmbeddingMethod
            Quantum embedding method the fragment is part of.
        fid : int
            Fragment ID.
        name : str
            Name of fragment.
        c_frag : (nAO, nFrag) array
            Fragment orbital coefficients.
        c_env : (nAO, nEnv) array
            Environment (non-fragment) orbital coefficients.
        fragment_type : {'IAO', 'Lowdin-AO', 'AO'}
            Fragment orbital type.
        atoms : list or int, optional
            Associated atoms. Default: None
        aos : list or int, optional
            Associated atomic orbitals. Default: None
        sym_factor : float, optional
            Symmetry factor (number of symmetry equivalent fragments). Default: 1.0.
        log : logging.Logger
            Logger object. If None, the logger of the `base` object is used. Default: None.

        Attributes
        ----------
        mol
        mf
        size
        nelectron
        id_name
        boundary_cond
        log : logging.Logger
            Logger object.
        base : QEmbeddingMethod
            Quantum embedding method, the fragment is part of.
        id : int
            Unique fragment ID.
        name : str
            Name of framgnet.
        c_frag : (nAO, nFrag) array
            Fragment orbital coefficients.
        c_env : (nAO, nEnv) array
            Environment (non-fragment) orbital coefficients.
        fragment_type : {'IAO', 'Lowdin-AO', 'AO'}
            Fragment orbital type.
        sym_factor : float
            Symmetry factor (number of symmetry equivalent fragments).
        atoms : list
            Atoms in fragment.
        aos : list
            Atomic orbitals in fragment
        coupled_fragments : list
            List of fragments, the current fragment is coupled to.
        """
        self.log = log or base.log
        self.id = fid
        self.name = name
        self.log.info("Initializing %s" % self)
        self.log.info("-------------%s" % (len(str(self))*"-"))

        # Options
        self.base = base
        if options is None:
            options = self.Options(**kwargs)
        else:
            options = options.replace(kwargs)
        options = options.replace(self.base.opts, select=NotSet)
        self.opts = options

        self.c_frag = c_frag
        self.c_env = c_env
        self.fragment_type = fragment_type
        self.sym_factor = self.opts.sym_factor
        # For some embeddings, it may be necessary to keep track of any associated atoms or basis functions (AOs)
        self.atoms = atoms
        self.aos = aos

        # Some output
        fmt = '  > %-24s %r'
        self.log.info(fmt, "Fragment type:", self.fragment_type)
        self.log.info(fmt, "Fragment orbitals:", self.size)
        self.log.info(fmt, "Symmetry factor:", self.sym_factor)
        self.log.info(fmt, "Number of electrons:", self.nelectron)
        if self.atoms is not None:
            self.log.info(fmt, "Associated atoms:", self.atoms)
        if self.aos is not None:
            self.log.info(fmt, "Associated AOs:", self.aos)

        # Final cluster active orbitals
        self.c_active_occ = None
        self.c_active_vir = None

        self.results = None


    def __repr__(self):
        keys = ['id', 'name', 'fragment_type', 'atoms', 'aos']
        fmt = ('%s(' + len(keys)*'%s: %r, ')[:-2] + ')'
        values = [self.__dict__[k] for k in keys]
        return fmt % (self.__class__.__name__, *[x for y in zip(keys, values) for x in y])

    def __str__(self):
        return '%s %d: %s' % (self.__class__.__name__, self.id, self.trimmed_name())

    @property
    def mol(self):
        return self.base.mol

    @property
    def mf(self):
        return self.base.mf

    @property
    def size(self):
        """Number of fragment orbitals."""
        return self.c_frag.shape[-1]

    @property
    def nelectron(self):
        """Number of mean-field electrons."""
        sc = np.dot(self.base.get_ovlp(), self.c_frag)
        ne = np.einsum("ai,ab,bi->", sc, self.mf.make_rdm1(), sc)
        return ne

    def trimmed_name(self, length=10, add_dots=True):
        """Fragment name trimmed to a given maximum length."""
        if len(self.name) <= length:
            return self.name
        if add_dots:
            return self.name[:(length-3)] + "..."
        return self.name[:length]

    @property
    def id_name(self):
        """Use this whenever a unique name is needed (for example to open a seperate file for each fragment)."""
        return "%s-%s" % (self.id, self.trimmed_name())

    @property
    def boundary_cond(self):
        return self.base.boundary_cond

    @property
    def c_active(self):
        if self.c_active_occ is None:
            return None
        return np.hstack((self.c_active_occ, self.c_active_vir))

    def couple_to_fragment(self, frag):
        if frag is self:
            raise RuntimeError("Cannot couple fragment with itself.")
        self.log.debugv("Coupling %s with %s", self, frag)
        self.opts.coupled_fragments.append(frag)

    def couple_to_fragments(self, frags):
        for frag in frags:
            self.couple_to_fragment(frag)

    def get_fragment_mf_energy(self):
        """Calculate the part of the mean-field energy associated with the fragment.

        Does not include nuclear-nuclear repulsion!
        """
        h1e = np.linalg.multi_dot((self.base.mo_coeff.T, self.mf.get_hcore(), self.base.mo_coeff))
        h1e += np.diag(self.base.mo_energy)
        p = self.get_fragment_projector(self.base.mo_coeff)
        h1e = np.dot(p, h1e)
        e_mf = np.sum(np.diag(h1e)[self.base.mo_occ>0])
        return e_mf


    def get_fragment_projector(self, coeff, ao_ptype='right', inverse=False):
        """Projector for one index of amplitudes local energy expression.

        Parameters
        ----------
        coeff : ndarray, shape(nAO, n)
            Occupied or virtual orbital coefficients.
        ao_ptype : {'right', 'left', 'symmetric'}, optional
            Defines were the projector is restricted to AO indices. Is only used
            of `self.fragment_type == 'AO'`. Default: 'right'.
        inverse : bool, optional
            Return 1-p instead. Default: False.

        Returns
        -------
        p : (n, n) array
            Projection matrix.
        """
        self.log.debugv("Get fragment projector type %s", self.fragment_type)
        if self.fragment_type.upper() == 'SITE':
            r = np.dot(coeff.T, self.c_frag)
            p = np.dot(r, r.T)
        if self.fragment_type.upper() in ('IAO', 'LOWDIN-AO'):
            r = np.linalg.multi_dot((coeff.T, self.base.get_ovlp(), self.c_frag))
            p = np.dot(r, r.T)
        if self.fragment_type.upper() == 'AO':
            if self.aos is None:
                raise ValueError("Cannot obtain local projector for fragment_type 'AO', if attribute `aos` is not set.")
            if ao_ptype == 'right':
                p = np.linalg.multi_dot((coeff.T, self.base.get_ovlp()[:,self.aos], self.c_frag[self.aos]))
            elif ao_ptype == 'right':
                p = np.linalg.multi_dot((coeff[self.aos].T, self.base.get_ovlp()[self.aos], self.c_frag))
            elif ao_ptype == 'symmetric':
                # Does this even make sense?
                shalf = scipy.linalg.fractional_matrix_power(self.get_ovlp, 0.5)
                assert np.allclose(s.half.imag, 0)
                shalf = shalf.real
                p = np.linalg.multi_dot((C.T, shalf[:,self.aos], s[self.aos], C))
        if inverse:
            p = np.eye(p.shape[-1]) - p
        return p


    def get_mo_occupation(self, *mo_coeff):
        """Get mean-field occupation numbers (diagonal of 1-RDM) of orbitals.

        Parameters
        ----------
        mo_coeff : ndarray, shape(N, M)
            Orbital coefficients.

        Returns
        -------
        occ : ndarray, shape(M)
            Occupation numbers of orbitals.
        """
        mo_coeff = np.hstack(mo_coeff)
        sc = np.dot(self.base.get_ovlp(), mo_coeff)
        occ = einsum('ai,ab,bi->i', sc, self.mf.make_rdm1(), sc)
        return occ


    def loop_fragments(self, exclude_self=False):
        """Loop over all fragments."""
        for frag in self.base.fragments:
            if (exclude_self and frag is self):
                continue
            yield frag


    def canonicalize_mo(self, *mo_coeff, eigvals=False):
        """Diagonalize Fock matrix within subspace.

        Parameters
        ----------
        *mo_coeff : ndarrays
            Orbital coefficients.
        eigenvalues : ndarray
            Return MO energies of canonicalized orbitals.

        Returns
        -------
        mo_canon : ndarray
            Canonicalized orbital coefficients.
        rot : ndarray
            Rotation matrix: np.dot(mo_coeff, rot) = mo_canon.
        """
        mo_coeff = np.hstack(mo_coeff)
        fock = np.linalg.multi_dot((mo_coeff.T, self.base.get_fock(), mo_coeff))
        mo_energy, rot = np.linalg.eigh(fock)
        mo_can = np.dot(mo_coeff, rot)
        mo_can = helper.orbital_sign_convention(mo_can)
        if eigvals:
            return mo_can, rot, mo_energy
        return mo_can, rot


    def diagonalize_cluster_dm(self, *mo_coeff, tol=1e-4):
        """Diagonalize cluster (fragment+bath) DM to get fully occupied and virtual orbitals.

        Parameters
        ----------
        *mo_coeff : ndarrays
            Orbital coefficients.
        tol : float, optional
            If set, check that all eigenvalues of the cluster DM are close
            to 0 or 1, with the tolerance given by tol. Default= 1e-4.

        Returns
        -------
        c_occclt : ndarray
            Occupied cluster orbitals.
        c_virclt : ndarray
            Virtual cluster orbitals.
        """
        #c_clt = np.hstack((self.c_frag, c_bath))
        c_clt = np.hstack(mo_coeff)
        sc = np.dot(self.base.get_ovlp(), c_clt)
        dm = np.linalg.multi_dot((sc.T, self.mf.make_rdm1(), sc)) / 2
        e, v = np.linalg.eigh(dm)
        if tol and not np.allclose(np.fmin(abs(e), abs(e-1)), 0, atol=tol, rtol=0):
            raise RuntimeError("Error while diagonalizing cluster DM: eigenvalues not all close to 0 or 1:\n%s", e)
        e, v = e[::-1], v[:,::-1]
        c_clt = np.dot(c_clt, v)
        nocc = sum(e >= 0.5)
        c_occclt, c_virclt = np.hsplit(c_clt, [nocc])
        return c_occclt, c_virclt


    def project_ref_orbitals(self, c_ref, c):
        """Project reference orbitals into available space in new geometry.

        The projected orbitals will be ordered according to their eigenvalues within the space.

        Parameters
        ----------
        c : ndarray
            Orbital coefficients.
        c_ref : ndarray
            Orbital coefficients of reference orbitals.
        """
        nref = c_ref.shape[-1]
        assert (nref > 0)
        assert (c.shape[-1] > 0)
        self.log.debug("Projecting %d reference orbitals into space of %d orbitals", nref, c.shape[-1])
        s = self.base.get_ovlp()
        # Diagonalize reference orbitals among themselves (due to change in overlap matrix)
        c_ref_orth = pyscf.lo.vec_lowdin(c_ref, s)
        assert (c_ref_orth.shape == c_ref.shape)
        # Diagonalize projector in space
        csc = np.linalg.multi_dot((c_ref_orth.T, s, c))
        p = np.dot(csc.T, csc)
        e, r = np.linalg.eigh(p)
        e, r = e[::-1], r[:,::-1]
        c = np.dot(c, r)

        return c, e


    # --- DMET
    # ========

    def make_dmet_bath(self, c_env, dm1=None, c_ref=None, nbath=None, tol=1e-5, verbose=True, reftol=0.8):
        """Calculate DMET bath, occupied environment and virtual environment orbitals.

        If c_ref is not None, complete DMET orbital space using active transformation of reference orbitals.

        TODO:
        * reftol should not be necessary - just determine how many DMET bath orbital N are missing
        from C_ref and take the N largest eigenvalues over the combined occupied and virtual
        eigenvalues.

        Parameters
        ----------
        c_env : (n(AO), n(env)) array
            MO-coefficients of environment orbitals.
        dm1 : (n(AO), n(AO)) array, optional
            Mean-field one-particle reduced density matrix in AO representation. If None, `self.mf.make_rdm1()` is used.
            Default: None.
        c_ref : ndarray, optional
            Reference DMET bath orbitals from previous calculation.
        nbath : int, optional
            Number of DMET bath orbitals. If set, the paramter `tol` is ignored. Default: None.
        tol : float, optional
            Tolerance for DMET orbitals in eigendecomposition of density-matrix. Default: 1e-5.
        reftol : float, optional
            Tolerance for DMET orbitals in projection of reference orbitals.

        Returns
        -------
        c_bath : (n(AO), n(bath)) array
            DMET bath orbitals.
        c_occenv : (n(AO), n(occ. env)) array
            Occupied environment orbitals.
        c_virenv : (n(AO), n(vir. env)) array
            Virtual environment orbitals.
        """

        # No environemnt -> no bath/environment orbitals
        if c_env.shape[-1] == 0:
            nao = c_env.shape[0]
            return np.zeros((nao, 0)), np.zeros((nao, 0)), np.zeros((nao, 0))

        # Divide by 2 to get eigenvalues in [0,1]
        sc = np.dot(self.base.get_ovlp(), c_env)
        if dm1 is None: dm1 = self.mf.make_rdm1()
        dm_env = np.linalg.multi_dot((sc.T, dm1, sc)) / 2
        try:
            eig, r = np.linalg.eigh(dm_env)
        except np.linalg.LinAlgError:
            eig, r = scipy.linalg.eigh(dm_env)
        # Sort: occ. env -> DMET bath -> vir. env
        eig, r = eig[::-1], r[:,::-1]
        if (eig.min() < -1e-9):
            self.log.warning("Min eigenvalue of env. DM = %.6e", eig.min())
        if ((eig.max()-1) > 1e-9):
            self.log.warning("Max eigenvalue of env. DM = %.6e", eig.max())
        c_env = np.dot(c_env, r)

        if nbath is not None:
            # Work out tolerance which leads to nbath bath orbitals. This overwrites `tol`.
            abseig = abs(eig[np.argsort(abs(eig-0.5))])
            low, up = abseig[nbath-1], abseig[nbath]
            if abs(low - up) < 1e-14:
                raise RuntimeError("Degeneracy in env. DM does not allow for clear identification of %d bath orbitals!\nabs(eig)= %r"
                        % (nbath, abseig[:nbath+5]))
            tol = (low + up)/2
            self.log.debugv("Tolerance for %3d bath orbitals= %.8g", nbath, tol)

        mask_bath = np.logical_and(eig >= tol, eig <= 1-tol)
        mask_occenv = (eig > 1-tol)
        mask_virenv = (eig < tol)
        nbath = sum(mask_bath)

        noccenv = sum(mask_occenv)
        nvirenv = sum(mask_virenv)
        self.log.info("DMET bath:  n(Bath)= %4d  n(occ-Env)= %4d  n(vir-Env)= %4d", nbath, noccenv, nvirenv)
        assert (nbath + noccenv + nvirenv == c_env.shape[-1])
        c_bath = c_env[:,mask_bath].copy()
        c_occenv = c_env[:,mask_occenv].copy()
        c_virenv = c_env[:,mask_virenv].copy()

        if verbose:
            # Orbitals in [print_tol, 1-print_tol] will be printed (even if they don't fall in the DMET tol range)
            print_tol = 1e-10
            # DMET bath orbitals with eigenvalue in [strong_tol, 1-strong_tol] are printed as strongly entangled
            strong_tol = 0.1
            limits = [print_tol, tol, strong_tol, 1-strong_tol, 1-tol, 1-print_tol]
            if np.any(np.logical_and(eig > limits[0], eig <= limits[-1])):
                names = [
                        "Unentangled vir. env. orbital",
                        "Weakly-entangled vir. bath orbital",
                        "Strongly-entangled bath orbital",
                        "Weakly-entangled occ. bath orbital",
                        "Unentangled occ. env. orbital",
                        ]
                self.log.info("Non-(0 or 1) eigenvalues (n) of environment DM:")
                for i, e in enumerate(eig):
                    name = None
                    for j, llim in enumerate(limits[:-1]):
                        ulim = limits[j+1]
                        if (llim < e and e <= ulim):
                            name = names[j]
                            break
                    if name:
                        self.log.info("  > %-34s  n= %12.6g  1-n= %12.6g", name, e, 1-e)

            # DMET bath analysis
            self.log.info("DMET bath character:")
            for i in range(c_bath.shape[-1]):
                ovlp = einsum('a,b,ba->a', c_bath[:,i], c_bath[:,i], self.base.get_ovlp())
                sort = np.argsort(-ovlp)
                ovlp = ovlp[sort]
                n = np.amin((len(ovlp), 6))     # Get the six largest overlaps
                labels = np.asarray(self.mol.ao_labels())[sort][:n]
                lines = [('%s= %.5f' % (labels[i].strip(), ovlp[i])) for i in range(n)]
                self.log.info("  > %2d:  %s", i+1, '  '.join(lines))

        # Calculate entanglement entropy
        entropy = np.sum(eig * (1-eig))
        entropy_bath = np.sum(eig[mask_bath] * (1-eig[mask_bath]))
        self.log.info("Entanglement entropy: total= %.6e  bath= %.6e  captured=  %.2f %%",
                entropy, entropy_bath, 100.0*entropy_bath/entropy)

        # Complete DMET orbital space using reference orbitals
        # NOT MAINTAINED!
        if c_ref is not None:
            nref = c_ref.shape[-1]
            self.log.debug("%d reference DMET orbitals given.", nref)
            nmissing = nref - nbath

            # DEBUG
            _, eig = self.project_ref_orbitals(c_ref, c_bath)
            self.log.debug("Eigenvalues of reference orbitals projected into DMET bath:\n%r", eig)

            if nmissing == 0:
                self.log.debug("Number of DMET orbitals equal to reference.")
            elif nmissing > 0:
                # Perform the projection separately for occupied and virtual environment space
                # Otherwise, it is not guaranteed that the additional bath orbitals are
                # fully (or very close to fully) occupied or virtual.
                # --- Occupied
                C_occenv, eig = self.project_ref_orbitals(c_ref, c_occenv)
                mask_occref = eig >= reftol
                mask_occenv = eig < reftol
                self.log.debug("Eigenvalues of projected occupied reference: %s", eig[mask_occref])
                if np.any(mask_occenv):
                    self.log.debug("Largest remaining: %s", max(eig[mask_occenv]))
                # --- Virtual
                c_virenv, eig = self.project_ref_orbitals(c_ref, c_virenv)
                mask_virref = eig >= reftol
                mask_virenv = eig < reftol
                self.log.debug("Eigenvalues of projected virtual reference: %s", eig[mask_virref])
                if np.any(mask_virenv):
                    self.log.debug("Largest remaining: %s", max(eig[mask_virenv]))
                # -- Update coefficient matrices
                c_bath = np.hstack((c_bath, c_occenv[:,mask_occref], c_virenv[:,mask_virref]))
                c_occenv = c_occenv[:,mask_occenv].copy()
                c_virenv = c_virenv[:,mask_virenv].copy()
                nbath = C_bath.shape[-1]
                self.log.debug("New number of occupied environment orbitals: %d", c_occenv.shape[-1])
                self.log.debug("New number of virtual environment orbitals: %d", c_virenv.shape[-1])
                if nbath != nref:
                    err = "Number of DMET bath orbitals=%d not equal to reference=%d" % (nbath, nref)
                    self.log.critical(err)
                    raise RuntimeError(err)
            else:
                err = "More DMET bath orbitals found than in reference!"
                self.log.critical(err)
                raise RuntimeError(err)

        return c_bath, c_occenv, c_virenv

    # --- Symmetry
    # ============

    def make_tsymmetric_fragments(self, tvecs, unit='Ang'):
        #if self.boundary_cond == 'open': return []

        mesh, tvecs = tsymmetry.get_mesh_tvecs(self.mol, tvecs, unit)
        self.log.debugv("nx= %d ny= %d nz= %d", *mesh)
        self.log.debugv("tvecs=\n%r", tvecs)

        ovlp = self.base.get_ovlp()
        sds = np.linalg.multi_dot((ovlp, self.mf.make_rdm1(), ovlp))
        dm0 = np.linalg.multi_dot((self.c_frag.T, sds, self.c_frag))

        fragments = []
        for dx in range(mesh[0]):
            for dy in range(mesh[1]):
                for dz in range(mesh[2]):
                    if abs(dx) + abs(dy) + abs(dz) == 0:
                        continue
                    t = dx*tvecs[0] + dy*tvecs[1] + dz*tvecs[2]
                    reorder = tsymmetry.reorder_aos(self.mol, t, unit='Bohr')[0]
                    if reorder is None:
                        self.log.error("No T-symmetric fragment found for translation [%d %d %d] of fragment %s", dx, dy, dz, self.name)
                        continue
                    name = '%s.t%d.%d.%d' % (self.name, dx, dy, dz)
                    c_frag = self.c_frag[reorder]
                    c_env = self.c_env[reorder]
                    # Check that fragment does not overlap with existing fragments:
                    for f in self.base.fragments:
                        ovlp = np.linalg.norm(np.linalg.multi_dot((f.c_frag.T, self.base.get_ovlp(), c_frag)))
                        if ovlp > 1e-10:
                            self.log.error("Translation [%d %d %d] of fragment %s overlaps with fragment %s (overlap= %.3e)!",
                                    dx, dy, dz, self.name, f.name, ovlp)
                    # Check that MF solution has lattice periodicity:
                    dm = np.linalg.multi_dot((c_frag.T, sds, c_frag))
                    err = np.linalg.norm(dm - dm0)
                    if err > 1e-10:
                        self.log.error("Mean-field not T-symmetric for translation [%d %d %d] of fragment %s (error= %.3e)!",
                                dx, dy, dz, self.name, err)
                    f = self.base.add_fragment(name, c_frag, c_env, fragment_type=self.fragment_type, options=self.opts, tsym_parent=self)
                    fragments.append(f)
                    # Copy results
                    f.results = self.results
                    if self.c_active_occ is not None:
                        f.c_active_occ = self.c_active_occ[reorder]
                        f.c_active_vir = self.c_active_vir[reorder]
        return fragments


    # --- Counterpoise
    # ================


    def make_counterpoise_mol(self, rmax, nimages=1, unit='A', **kwargs):
        """Make molecule object for counterposise calculation.

        WARNING: This has only been tested for periodic systems so far!

        Parameters
        ----------
        rmax : float
            All atom centers within range `rmax` are added as ghost-atoms in the counterpoise correction.
        nimages : int, optional
            Number of neighboring unit cell in each spatial direction. Has no effect in open boundary
            calculations. Default: 5.
        unit : ['A', 'B']
            Unit for `rmax`, either Angstrom (`A`) or Bohr (`B`).
        **kwargs :
            Additional keyword arguments for returned PySCF Mole/Cell object.

        Returns
        -------
        mol_cp : pyscf.gto.Mole or pyscf.pbc.gto.Cell
            Mole or Cell object with periodic boundary conditions removed
            and with ghost atoms added depending on `rmax` and `nimages`.
        """
        if len(self.atoms) > 1:
            raise NotImplementedError()
        import vayesta.misc
        return vayesta.misc.counterpoise.make_mol(self.mol, self.atoms[1], rmax=rmax, nimages=nimages, unit=unit, **kwargs)