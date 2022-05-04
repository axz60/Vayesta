import logging
import itertools

import numpy as np
import scipy
import scipy.spatial

import vayesta
import vayesta.core
from vayesta.core.util import *


log = logging.getLogger(__name__)

def compare_atoms(mol, atom1, atom2, check_labels=False, check_basis=True):
    """Compare atom symbol and (optionally) basis between atom1 and atom2."""
    if check_labels:
        type1 = mol.atom_symbol(atom1)
        type2 = mol.atom_symbol(atom2)
    else:
        type1 = mol.atom_pure_symbol(atom1)
        type2 = mol.atom_pure_symbol(atom2)
    if (type1 != type2):
        return False
    if not check_basis:
        return True
    bas1 = mol._basis[mol.atom_symbol(atom1)]
    bas2 = mol._basis[mol.atom_symbol(atom2)]
    return (bas1 == bas2)

def get_closest_atom(mol, coords):
    """pos in internal coordinates."""
    dists = np.linalg.norm(mol.atom_coords()-coords, axis=1)
    idx = np.argmin(dists)
    return idx, dists[idx]


class SymmetryOperation:

    def __init__(self, mol, xtol=1e-8):
        self.mol = mol
        self.xtol = xtol

    def __call__(self):
        raise AbstractMethodError()

    @property
    def natom(self):
        return self.mol.natm

    @property
    def nao(self):
        return self.mol.nao

class SymmetryIdentity(SymmetryOperation):

    def __call__(self, a, **kwargs):
        return a

    def change_mol(self, mol):
        return self

class SymmetryRotation(SymmetryOperation):

    def __init__(self, mol, order, vector, origin=np.zeros(3)):
        super().__init__(mol)
        self.order = order
        self.vector = (vector / np.linalg.norm(vector))
        self.origin = origin

    def as_matrix(self):
        vec = self.vector * (2*np.pi/self.order)
        return scipy.spatial.transform.Rotation.from_rotvec(vec).as_matrix()

    def get_atom_reorder(self, check_labels=False, check_basis=True):
        """Reordering of atoms for a given rotation.

        Parameters
        ----------

        Returns
        -------
        reorder: list
        inverse: list
        """
        reorder = np.full((self.natom,), -1, dtype=int)
        inverse = np.full((self.natom,), -1, dtype=int)
        for atom0, r0 in enumerate(self.mol.atom_coords()):
            r1 = np.dot((r0 - self.origin[None]), self.as_matrix()) + self.origin[None]
            atom1, dist = get_closest_atom(self.mol, r1)
            if dist > self.xtol:
                return None, None
            if not compare_atoms(self.mol, atom0, atom1, check_labels=check_labels, check_basis=check_basis):
                return None, None
            reorder[atom1] = atom0
            inverse[atom0] = atom1
        assert (not np.any(reorder == -1))
        assert (not np.any(inverse == -1))

        assert np.all(np.arange(self.natom)[reorder][inverse] == np.arange(self.natom))

        return reorder, inverse


class SymmetryTranslation(SymmetryOperation):

    def __init__(self, cell, vector, boundary=None, atom_reorder=None, ao_reorder=None):
        super().__init__(cell)
        self.vector = np.asarray(vector)

        if boundary is None:
            boundary = getattr(cell, 'boundary', 'PBC')
        if np.ndim(boundary) == 0:
            boundary = 3*[boundary]
        elif np.ndim(boundary) == 1 and len(boundary) == 2:
            boundary = [boundary[0], boundary[1], 'PBC']
        self.boundary = boundary

        # Atom reorder
        self.atom_reorder_phases = None
        self.ao_reorder_phases = None
        if atom_reorder is None:
            atom_reorder, _, self.atom_reorder_phases = self.get_atom_reorder()
        self.atom_reorder = atom_reorder
        assert (self.atom_reorder is not None)
        # AO reorder
        if ao_reorder is None:
            ao_reorder, _, self.ao_reorder_phases = self.get_ao_reorder()
        self.ao_reorder = ao_reorder
        assert (self.ao_reorder is not None)

    def __call__(self, a, axis=0):
        if hasattr(axis, '__len__'):
            for ax in axis:
                a = self.apply_to_aos(a, axis=ax)
            return a
        return self.apply_to_aos(a, axis=axis)

    def inverse(self):
        return type(self)(self.cell, -self.vector, boundary=self.boundary, atom_reorder=np.argsort(self.atom_reorder))

    def change_mol(self, mol):
        return SymmetryTranslation(mol, vector=self.vector, boundary=self.boundary)

    def apply_to_aos(self, a, axis=0):
        """Apply symmetry operation along AO axis."""
        if isinstance(a, (tuple, list)):
            return tuple([self.__call__(x, axis=axis) for x in a])
        bc = tuple(axis*[None] + [slice(None, None, None)] + (a.ndim-axis-1)*[None])
        if self.ao_reorder_phases is None:
            return np.take(a, self.ao_reorder, axis=axis)
        return np.take(a, self.ao_reorder, axis=axis) * self.ao_reorder_phases[bc]

    @property
    def cell(self):
        return self.mol

    @property
    def lattice_vectors(self):
        return self.mol.lattice_vectors()

    @property
    def inv_lattice_vectors(self):
        return np.linalg.inv(self.lattice_vectors)

    @property
    def boundary_phases(self):
        return np.asarray([1 if (b.lower() == 'pbc') else -1 for b in self.boundary])

    @property
    def vector_xyz(self):
        """Translation vector in real-space coordinates (unit = Bohr)."""
        latvecs = self.lattice_vectors
        vector_xyz = np.dot(self.lattice_vectors, self.vector)
        return vector_xyz

    @property
    def inverse_atom_reorder(self):
        if self.atom_reorder is None:
            return None
        return np.argsort(self.atom_reorder)

    def get_atom_reorder(self, check_labels=False, check_basis=True):
        """Reordering of atoms for a given translation.

        Parameters
        ----------

        Returns
        -------
        reorder: list
        inverse: list
        phases: list
        """
        atom_coords_abc = np.dot(self.mol.atom_coords(), self.inv_lattice_vectors)

        def get_atom_at(pos):
            """pos in internal coordinates."""
            for dx, dy, dz in itertools.product([0,-1,1], repeat=3):
                if self.cell.dimension in (1, 2) and (dz != 0): continue
                if self.cell.dimension == 1 and (dy != 0): continue
                dr = np.asarray([dx, dy, dz])
                phase = np.product(self.boundary_phases[dr!=0])
                dists = np.linalg.norm(atom_coords_abc + dr - pos, axis=1)
                idx = np.argmin(dists)
                if (dists[idx] < self.xtol):
                    return idx, phase
            return None, None

        reorder = np.full((self.natom,), -1)
        inverse = np.full((self.natom,), -1)
        phases = np.full((self.natom,), 0)
        for atom0, coords0 in enumerate(atom_coords_abc):
            atom1, phase = get_atom_at(coords0 + self.vector)
            if atom1 is None:
                return None, None, None
            if not compare_atoms(self.mol, atom0, atom1, check_labels=check_labels, check_basis=check_basis):
                return None, None, None
            reorder[atom1] = atom0
            inverse[atom0] = atom1
            phases[atom0] = phase
        assert (not np.any(reorder == -1))
        assert (not np.any(inverse == -1))
        assert (not np.any(phases == 0))

        assert np.all(np.arange(self.natom)[reorder][inverse] == np.arange(self.natom))

        return reorder, inverse, phases

    def get_ao_reorder(self, atom_reorder=None, atom_reorder_phases=None):
        if atom_reorder is None:
            atom_reorder = self.atom_reorder
        if atom_reorder_phases is None:
            atom_reorder_phases = self.atom_reorder_phases
        if atom_reorder is None:
            return None, None, None
        aoslice = self.cell.aoslice_by_atom()[:,2:]
        reorder = np.full((self.cell.nao,), -1)
        inverse = np.full((self.cell.nao,), -1)
        if atom_reorder_phases is not None:
            phases = np.full((self.cell.nao,), 0)
        else:
            phases = None
        for atom0 in range(self.natom):
            atom1 = atom_reorder[atom0]
            aos0 = list(range(aoslice[atom0,0], aoslice[atom0,1]))
            aos1 = list(range(aoslice[atom1,0], aoslice[atom1,1]))
            reorder[aos0[0]:aos0[-1]+1] = aos1
            inverse[aos1[0]:aos1[-1]+1] = aos0
            if atom_reorder_phases is not None:
                phases[aos0[0]:aos0[-1]+1] =  atom_reorder_phases[atom1]
        assert not np.any(reorder == -1)
        assert not np.any(inverse == -1)
        if atom_reorder_phases is not None:
            assert not np.any(phases == 0)

        assert np.all(np.arange(self.nao)[reorder][inverse] == np.arange(self.nao))

        return reorder, inverse, phases

if __name__ == '__main__':

    def test_translation():
        import pyscf
        import pyscf.pbc
        import pyscf.pbc.gto
        import pyscf.pbc.scf
        import pyscf.pbc.tools
        import pyscf.pbc.df

        cell = pyscf.pbc.gto.Cell()
        cell.a = 3*np.eye(3)
        cell.atom = 'He 0 0 0'
        cell.unit = 'Bohr'
        cell.basis = 'def2-svp'
        #cell.basis = 'sto-3g'
        cell.build()
        #cell.dimension = 2

        sc = [1,1,2]
        cell = pyscf.pbc.tools.super_cell(cell, sc)

        t = Translation(cell, [0, 0, 1/2])

        df = pyscf.pbc.df.GDF(cell)
        df.auxbasis = 'def2-svp-ri'
        df.build()

        #print(t.ao_reorder)
        #aux_reorder = t.get_ao_reorder(cell=df.auxcell)[0]
        #print(aux_reorder)
        taux = t.change_cell(df.auxcell)
        print(t.ao_reorder)
        print(taux.ao_reorder)

        #print(trans.atom_reorder)
        #print(trans.ao_reorder)
        #trans = Translation(cell, [0,1/5,2/3])
        #trans = Translation(cell, [0,3/5,2/3])

        #mo0 = np.eye(cell.nao)
        #mo1 = trans(mo0)

    def test_rotation():
        import pyscf
        import pyscf.gto
        import vayesta.misc
        import vayesta.misc.molecules

        mol = pyscf.gto.Mole()
        mol.atom = vayesta.misc.molecules.arene(6)
        mol.build()

        vec = np.asarray([0, 0, 1])
        op = SymmetryRotation(mol, 6, vec)

        reorder, inv = op.get_atom_reorder()
        print(reorder)
        print(inv)



    test_rotation()