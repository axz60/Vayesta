import unittest
import numpy as np

import pyscf
import pyscf.scf
import pyscf.cc

import vayesta
import vayesta.ewf
from vayesta.tests.cache import moles


class T_Amplitudes_Tests(unittest.TestCase):
    key = 'h2o_ccpvdz'

    @classmethod
    def setUpClass(cls):
        # CCSD
        cls.ccsd = pyscf.cc.CCSD(moles[cls.key]['rhf'])
        cls.ccsd.kernel()
        assert cls.ccsd.converged
        cls.dm1 = cls.ccsd.make_rdm1()
        cls.dm2 = cls.ccsd.make_rdm2()

        # Emb-CCSD
        cls.ecc = vayesta.ewf.EWF(moles[cls.key]['rhf'], bath_type='full', make_rdm1=True, make_rdm2=True)
        cls.ecc.iao_fragmentation()
        cls.ecc.add_all_atomic_fragments()
        cls.ecc.kernel()

    @classmethod
    def tearDownClass(cls):
        del cls.ccsd, cls.ecc, cls.dm1, cls.dm2

    def test_t1(self):
        """Test T1 amplitudes.
        """
        atol = 1e-8
        t1 = self.ecc.get_global_t1()
        self.assertIsNone(np.testing.assert_allclose(t1, self.ccsd.t1, atol=atol))

    def test_t2(self):
        """Test T2 amplitudes.
        """
        atol = 1e-8
        t2 = self.ecc.get_global_t2()
        self.assertIsNone(np.testing.assert_allclose(t2, self.ccsd.t2, atol=atol))

    def test_t12(self):
        """Test T1 and T2 amplitudes.
        """
        atol = 1e-8
        t1 = self.ecc.get_global_t1()
        t2 = self.ecc.get_global_t2()
        self.assertIsNone(np.testing.assert_allclose(t1, self.ccsd.t1, atol=atol))
        self.assertIsNone(np.testing.assert_allclose(t2, self.ccsd.t2, atol=atol))

    def test_dm1(self):
        """Test 1RDM
        """
        atol = 1e-8
        dm1 = self.ecc.make_rdm1_ccsd()
        self.assertIsNone(np.testing.assert_allclose(dm1, self.dm1, atol=atol))

    def test_dm2(self):
        """Test 2RDM
        """
        atol = 1e-8
        dm2 = self.ecc.make_rdm2_ccsd()
        self.assertIsNone(np.testing.assert_allclose(dm2, self.dm2, atol=atol))


class T_Amplitudes_UHF_Tests(unittest.TestCase):
    key = 'h2o_ccpvdz'

    @classmethod
    def setUpClass(cls):
        # CCSD
        cls.ccsd = pyscf.cc.UCCSD(moles[cls.key]['uhf'])
        cls.ccsd.conv_tol = 1e-12
        cls.ccsd.conv_tol_normt = 1e-6
        cls.ccsd.kernel()
        assert cls.ccsd.converged
        cls.dm1 = cls.ccsd.make_rdm1()
        cls.dm2 = cls.ccsd.make_rdm2()

        # Emb-UCCSD
        opts = {'conv_tol': 1e-12, 'conv_tol_normt': 1e-6}
        cls.ecc = vayesta.ewf.EWF(moles[cls.key]['uhf'], bath_type='full', solver_options=opts)
        cls.ecc.iao_fragmentation()
        cls.ecc.add_all_atomic_fragments()
        cls.ecc.kernel()

    @classmethod
    def tearDownClass(cls):
        del cls.ccsd, cls.ecc, cls.dm1, cls.dm2

    def test_t1(self):
        """Test T1 amplitudes.
        """
        atol = 1e-6
        t1a, t1b = self.ecc.get_t1()
        self.assertIsNone(np.testing.assert_allclose(t1a, self.ccsd.t1[0], atol=atol))
        self.assertIsNone(np.testing.assert_allclose(t1b, self.ccsd.t1[1], atol=atol))

    def test_t2(self):
        """Test T2 amplitudes.
        """
        atol = 1e-6
        t2aa, t2ab, t2bb = self.ecc.get_t2()
        self.assertIsNone(np.testing.assert_allclose(t2aa, self.ccsd.t2[0], atol=atol))
        self.assertIsNone(np.testing.assert_allclose(t2ab, self.ccsd.t2[1], atol=atol))
        self.assertIsNone(np.testing.assert_allclose(t2bb, self.ccsd.t2[2], atol=atol))

    def test_t12(self):
        """Test T1 and T2 amplitudes.
        """
        atol = 1e-6
        t1a, t1b = self.ecc.get_global_t1()
        t2aa, t2ab, t2bb = self.ecc.get_global_t2()
        self.assertIsNone(np.testing.assert_allclose(t1a, self.ccsd.t1[0], atol=atol))
        self.assertIsNone(np.testing.assert_allclose(t1b, self.ccsd.t1[1], atol=atol))
        self.assertIsNone(np.testing.assert_allclose(t2aa, self.ccsd.t2[0], atol=atol))
        self.assertIsNone(np.testing.assert_allclose(t2ab, self.ccsd.t2[1], atol=atol))
        self.assertIsNone(np.testing.assert_allclose(t2bb, self.ccsd.t2[2], atol=atol))

    # TODO
    #def test_dm1(self):
    #    """Test 1RDM
    #    """
    #    atol = 1e-8
    #    dm1a, dm1b = self.ecc.make_rdm1_ccsd()
    #    self.assertIsNone(np.testing.assert_allclose(dm1a, self.dm1[0], atol=atol))
    #    self.assertIsNone(np.testing.assert_allclose(dm1b, self.dm1[1], atol=atol))

    #def test_dm2(self):
    #    """Test 2RDM
    #    """
    #    atol = 1e-8
    #    dm2aa, dm2ab, dm2bb = self.ecc.make_rdm2_ccsd()
    #    self.assertIsNone(np.testing.assert_allclose(dm2aa, self.dm2[0], atol=atol))
    #    self.assertIsNone(np.testing.assert_allclose(dm2ab, self.dm2[1], atol=atol))
    #    self.assertIsNone(np.testing.assert_allclose(dm2bb, self.dm2[2], atol=atol))


if __name__ == '__main__':
    print('Running %s' % __file__)
    unittest.main()