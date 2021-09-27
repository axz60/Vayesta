import numpy as np
from .orbitals import Orbitals

class ActiveSpace:

    def __init__(self, mf, c_active_occ, c_active_vir, c_frozen_occ=None, c_frozen_vir=None):
        self.mf = mf
        assert (self.norb == (self.nocc + self.nvir))
        # Active
        self._active_occ = Orbitals(c_active_occ, name="active-occupied")
        self._active_vir = Orbitals(c_active_vir, name="active-virtual")
        # Frozen
        if c_frozen_occ is not None:
            self._frozen_occ = Orbitals(c_frozen_occ, name="frozen-occupied")
        else:
            self._frozen_occ = None
        if c_frozen_vir is not None:
            self._frozen_vir = Orbitals(c_frozen_vir, name="frozen-virtual")
        else:
            self._frozen_vir = None

    # --- Mean-field:

    @property
    def mol(self):
        """PySCF Mole or Cell object."""
        return self.mf.mol

    @property
    def nao(self):
        """Number of atomic orbitals."""
        return self.mol.nao_nr()

    @property
    def norb(self):
        """Total number of molecular orbitals in the system."""
        return len(self.mf.mo_energy)

    @property
    def nocc(self):
        """Total number of occupied molecular orbitals in the system."""
        return np.count_nonzero(self.mf.mo_occ > 0)

    @property
    def nvir(self):
        """Total number of virtual molecular orbitals in the system."""
        return np.count_nonzero(self.mf.mo_occ == 0)

    # --- Sizes:

    @property
    def norb_active(self):
        """Number of active orbitals."""
        return (self.nocc_active + self.nvir_active)

    @property
    def nocc_active(self):
        """Number of active occupied orbitals."""
        return self._active_occ.size

    @property
    def nvir_active(self):
        """Number of active virtual orbitals."""
        return self._active_vir.size

    @property
    def norb_frozen(self):
        """Number of frozen orbitals."""
        return (self.norb - self.norb_active)

    @property
    def nocc_frozen(self):
        """Number of frozen occupied orbitals."""
        return (self.nocc - self.nocc_active)

    @property
    def nvir_frozen(self):
        """Number of frozen virtual orbitals."""
        return (self.nvir - self.nvir_active)

    # --- Electron numbers:

    @property
    def nelectron(self):
        """Total number of electrons in the system."""
        return 2*self.nocc

    @property
    def nelectron_active(self):
        """Number of active electrons in the system."""
        return 2*self.nocc_active

    @property
    def nelectron_frozen(self):
        """Number of frozen electrons in the system."""
        return 2*self.nocc_frozen

    # --- Combined spaces:

    @property
    def active(self):
        active = (self._active_occ + self._active_vir)
        active.occupied = self._active_occ
        active.virtual = self._active_vir
        return active

    @property
    def frozen(self):
        frozen = (self._frozen_occ + self._frozen_vir)
        frozen.occupied = self._frozen_occ
        frozen.virtual = self._frozen_vir
        return frozen

    @property
    def occupied(self):
        #occupied = (self._active_occ + self._frozen_occ)
        occupied = (self._frozen_occ + self._active_occ)
        occupied.active = self._active_occ
        occupied.frozen = self._frozen_occ
        return occupied

    @property
    def virtual(self):
        virtual = (self._active_vir + self._frozen_vir)
        virtual.active = self._active_occ
        virtual.frozen = self._frozen_occ
        return virtual

    @property
    def all(self):
        return (self._frozen_occ + self._active_occ, self._active_vir, self._frozen_vir)

    #@property
    #def coeff(self):
    #    return self.all.coeff

    #@property
    #def size(self):
    #    return self.all.size

    # --- Other:

    def log_sizes(self, logger, header=None):
        if header:
            logger(header)
            logger(len(header)*'-')
        logger("             Active                    Frozen")
        logger("             -----------------------   -----------------------")
        fmt = '  %-8s' + 2*'   %5d / %5d (%6.1f%%)'
        get_sizes = lambda a, f, n : (a, n, 100*a/n, f, n, 100*f/n)
        logger(fmt, "Occupied", *get_sizes(self.nocc_active, self.nocc_frozen, self.nocc))
        logger(fmt, "Virtual", *get_sizes(self.nvir_active, self.nvir_frozen, self.nvir))
        logger(fmt, "Total", *get_sizes(self.norb_active, self.norb_frozen, self.norb))


##class Cluster:
##
##    def __init__(self, mf, c_active_occ, c_active_vir, c_frozen_occ, c_frozen_vir, log, sym_op=None, sym_parent=None):
##        self.mf = mf
##        self.log = log
##        self._c_active_occ = c_active_occ
##        self._c_active_vir = c_active_vir
##        self._c_frozen_occ = c_frozen_occ
##        self._c_frozen_vir = c_frozen_vir
##        self.sym_op = sym_op
##        self.sym_parent = sym_parent
##
##    # --- Mean-field:
##
##    @property
##    def mol(self):
##        """PySCF Mole or Cell object."""
##        return self.mf.mol
##
##    @property
##    def nao(self):
##        """Number of atomic orbitals."""
##        return self.mol.nao_nr()
##
##    #@property
##    #def mo_coeff(self):
##    #    return self.mf.mo_coeff
##
##    # --- Sizes:
##
##    @property
##    def norb(self):
##        """Total number of occupied orbitals in the system."""
##        return self.nocc + self.nvir
##
##    @property
##    def nocc(self):
##        """Total number of occupied orbitals in the system."""
##        return self.nocc_active + self.nocc_frozen
##
##    @property
##    def nvir(self):
##        """Total number of virtual orbitals in the system."""
##        return self.nvir_active + self.nvir_frozen
##
##    @property
##    def norb_active(self):
##        """Number of active orbitals."""
##        return (self.nocc_active + self.nvir_active)
##
##    @property
##    def nocc_active(self):
##        """Number of active occupied orbitals."""
##        return self.c_active_occ.shape[-1]
##
##    @property
##    def nvir_active(self):
##        """Number of active virtual orbitals."""
##        return self.c_active_vir.shape[-1]
##
##    @property
##    def norb_frozen(self):
##        """Number of frozen orbitals."""
##        return (self.nocc_frozen + self.nvir_frozen)
##
##    @property
##    def nocc_frozen(self):
##        """Number of frozen occupied orbitals."""
##        return self.c_frozen_occ.shape[-1]
##
##    @property
##    def nvir_frozen(self):
##        """Number of frozen virtual orbitals."""
##        return self.c_frozen_vir.shape[-1]
##
##    # --- Electrons:
##
##    @property
##    def nelectron(self):
##        """Total number of electrons in the system."""
##        return 2*self.nocc
##
##    @property
##    def nelectron_active(self):
##        """Number of active electrons in the system."""
##        return 2*self.nocc_active
##
##    @property
##    def nelectron_frozen(self):
##        """Number of frozen electrons in the system."""
##        return 2*self.nocc_frozen
##
##    # --- Orbital coefficients:
##
##    @property
##    def c_active(self):
##        """Active orbital coefficients."""
##        if self.c_active_occ is None:
##            return None
##        return hstack(self.c_active_occ, self.c_active_vir)
##
##    @property
##    def c_active_occ(self):
##        """Active occupied orbital coefficients."""
##        if self.sym_parent is None:
##            return self._c_active_occ
##        else:
##            return self.sym_op(self.sym_parent.c_active_occ)
##
##    @property
##    def c_active_vir(self):
##        """Active virtual orbital coefficients."""
##        if self.sym_parent is None:
##            return self._c_active_vir
##        else:
##            return self.sym_op(self.sym_parent.c_active_vir)
##
##    @property
##    def c_frozen(self):
##        """Frozen orbital coefficients."""
##        if self.c_frozen_occ is None:
##            return None
##        return hstack(self.c_frozen_occ, self.c_frozen_vir)
##
##    @property
##    def c_frozen_occ(self):
##        """Frozen occupied orbital coefficients."""
##        if self.sym_parent is None:
##            return self._c_frozen_occ
##        else:
##            return self.sym_op(self.sym_parent.c_frozen_occ)
##
##    @property
##    def c_frozen_vir(self):
##        """Frozen virtual orbital coefficients."""
##        if self.sym_parent is None:
##            return self._c_frozen_vir
##        else:
##            return self.sym_op(self.sym_parent.c_frozen_vir)
##
##    def log_sizes(self, log, header=None):
##        if header:
##            log.info(header)
##            log.info(len(header)*'-')
##        log.info("             Active                    Frozen")
##        log.info("             -----------------------   -----------------------")
##        #log.info(13*' ' + "%-23s   %s", "Active", "Frozen")
##        #log.info(13*' ' + "%-23s   %s", 23*'-', 23*'-')
##        fmt = '  %-8s' + 2*'   %5d / %5d (%6.1f%%)'
##        get_sizes = lambda a, f, n : (a, n, 100*a/n, f, n, 100*f/n)
##        log.info(fmt, "Occupied", *get_sizes(self.nocc_active, self.nocc_frozen, self.nocc))
##        log.info(fmt, "Virtual", *get_sizes(self.nvir_active, self.nvir_frozen, self.nvir))
##        log.info(fmt, "Total", *get_sizes(self.norb_active, self.norb_frozen, self.norb))
