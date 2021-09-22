import numpy as np

import pyscf
import pyscf.lo

from vayesta.core.util import *
from vayesta.core.fragmentation.iao import IAO_Fragmentation

class IAOPAO_Fragmentation(IAO_Fragmentation):

    name = "IAO/PAO"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Order according to AOs:
        self.order = None
        iaopao_labels = self.get_labels()
        ao_labels = self.mol.ao_labels(None)
        order = []
        for l in ao_labels:
            idx = iaopao_labels.index(l)
            order.append(idx)
        assert np.all(np.asarray(iaopao_labels)[order] == ao_labels)
        self.order = order

    def get_coeff(self, order=None):
        """Make projected atomic orbitals (PAOs)."""
        if order is None: order = self.order
        iao_coeff = super().get_coeff(add_virtuals=False)

        core, valence, rydberg = pyscf.lo.nao._core_val_ryd_list(self.mol)
        # In case a minimal basis set is used:
        if not rydberg:
            return np.zeros((self.nao, 0))

        # "Representation of Rydberg-AOs in terms of AOs"
        pao_coeff = np.eye(self.nao)[:,rydberg]
        # Project AOs onto non-IAO space:
        # (S^-1 - C.CT) . S = (1 - C.CT.S)
        ovlp = self.get_ovlp()
        p_pao = np.eye(self.nao) - dot(iao_coeff, iao_coeff.T, ovlp)
        pao_coeff = np.dot(p_pao, pao_coeff)

        # Orthogonalize PAOs:
        x, e_min = self.get_lowdin_orth_x(pao_coeff, ovlp)
        self.log.debug("Lowdin orthogonalization of PAOs: n(in)= %3d -> n(out)= %3d , e(min)= %.3e",
                x.shape[0], x.shape[1], e_min)
        if e_min < 1e-12:
            self.log.warning("Small eigenvalue in Lowdin-orthogonalization: %.3e !", e_min)
        pao_coeff = np.dot(pao_coeff, x)

        coeff = np.hstack((iao_coeff, pao_coeff))
        assert (coeff.shape[-1] == self.mf.mo_coeff.shape[-1])
        # Test orthogonality of IAO+PAO
        self.check_orth(coeff, "IAO+PAO")

        if order is not None:
            return coeff[:,order]
        return coeff

    def get_labels(self, order=None):
        if order is None: order = self.order
        iao_labels = super().get_labels()
        core, valence, rydberg = pyscf.lo.nao._core_val_ryd_list(self.mol)
        pao_labels = [tuple(x) for x in np.asarray(self.mol.ao_labels(None), dtype=tuple)[rydberg]]
        labels = iao_labels + pao_labels
        if order is not None:
            return [tuple(l) for l in np.asarray(labels)[order]]
        return labels

    def search_labels(self, labels):
        return self.mol.search_ao_label(labels)

if __name__ == '__main__':

    import logging
    log = logging.getLogger(__name__)

    import pyscf
    import pyscf.gto
    import pyscf.scf

    mol = pyscf.gto.Mole()
    mol.atom = 'O 0 0 -1.2 ; C 0 0 0 ; O 0 0 1.2'
    mol.basis = 'cc-pVDZ'
    mol.build()

    mf = pyscf.scf.RHF(mol)
    mf.kernel()

    iaopao = IAOPAO_Fragmentation(mf, log)

    ao_labels = mol.ao_labels(None)
    print("Atomic order")
    for i, l in enumerate(iaopao.get_labels()):
        print("%30r   vs  %30r" % (l, ao_labels[i]))
