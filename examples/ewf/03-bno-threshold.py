import pyscf
import pyscf.gto
import pyscf.scf

import vayesta
import vayesta.ewf

mol = pyscf.gto.Mole()
mol.atom = ['Li 0.0 0.0 0.0', 'H 0.0, 0.0, 1.4']
mol.basis = 'aug-cc-pvdz'
mol.verbose = 10
mol.output = 'pyscf_out.txt'
mol.build()

# Hartree-Fock
mf = pyscf.scf.RHF(mol)
mf.kernel()

ecc = vayesta.ewf.EWF(mf)
ecc.make_all_atom_fragments()
ecc.kernel(bno_threshold=[1e-3, 1e-4, 1e-5, 1e-6, 1e-7, 1e-8])

print("E%-11s %+16.8f Ha" % ('(HF)=', mf.e_tot))
# ecc.e_tot and ecc.e_corr return the most accurate result (here 1e-8):
print("E%-11s %+16.8f Ha" % ('(EWF-CCSD)=', ecc.e_tot))

# ecc.get_energies() returns all results, in order of increasing BNO threshold:
print(ecc.get_energies())