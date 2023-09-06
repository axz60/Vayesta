import numpy as np
from vayesta.core.util import einsum, dot
from vayesta.core.vlog import NoLogger
import pyscf.lib
from vayesta.core.ao2mo import kao2gmo_cderi


class BosonicHamiltonianProjector:
    def __init__(self, cluster, mo_cderi_getter, mf, fock=None, log=None, kdf=None):
        self.cluster = cluster
        if cluster.bosons is None:
            raise ValueError("Cluster has no defined bosons to generate interactions for!")
        # Functions to get
        self.mo_cderi_getter = mo_cderi_getter
        self.mf = mf
        self.kdf = kdf
        self.fock = fock or mf.get_fock()
        assert self.cluster.inc_bosons
        self._cderi_clus = None
        self._cderi_bos = None
        self._cderi_glob = None
        self.log = log or NoLogger()

    @property
    def bcluster(self):
        return self.cluster.bosons

    @property
    def qba_basis(self):
        return self.bcluster.forbitals

    @property
    def cderi_clus(self):
        if self._cderi_clus is None:
            c_active = self.cluster.c_active
            if c_active[0].ndim == 1:
                cderi, cderi_neg = self.mo_cderi_getter(c_active)
                self._cderi_clus = ((cderi, cderi), (cderi_neg, cderi_neg))
            else:
                cderia, cderi_nega = self.mo_cderi_getter(c_active[0])
                cderib, cderi_negb = self.mo_cderi_getter(c_active[1])
                self._cderi_clus = ((cderia, cderib), (cderi_nega, cderi_negb))
        return self._cderi_clus

    @property
    def cderi_bos(self):
        if self._cderi_bos is None:
            if self.kdf is None:
                # Can just loop over density cderis in AO basis.
                rbos = sum(self.bcluster.coeff_3d_ao)
                cderi = np.zeros((self.naux, self.bcluster.nbos))
                cderi_neg = None
                for blk, lab in self._loop_df():
                    if blk is not None:
                        cderi[blk] = einsum("Lab,nab->Ln", lab, rbos)
                    else:
                        cderi_neg = einsum("Lab,nab->Ln", lab, rbos)
            else:
                # Using unfolded mean-field; need to go via global cderis.
                cderi, cderi_neg = self.cderi_glob_ov

                if cderi[0].ndim == 3:
                    rbosa, rbosb = self.bcluster.coeff_ex_3d
                    cderia, cderib = cderi
                    cderia_neg, cderib_neg = cderi_neg

                    cderi = einsum("Lia,nia->Ln", cderia, rbosa) + einsum("Lia,nia->Ln", cderib, rbosb)
                    cderi_neg = None
                    if cderia_neg is not None:
                        cderi_neg = einsum("Lia,nia->Ln", cderia_neg, rbosa) + einsum("Lia,nia->Ln", cderib_neg, rbosb)
                else:
                    rbos = sum(self.bcluster.coeff_ex_3d)
                    cderi = einsum("Lia,nia->Ln", cderi, rbos)
                    if cderi_neg is not None:
                        cderi_neg = einsum("Lia,nia->Ln", cderi_neg, rbos)

            self._cderi_bos = (cderi, cderi_neg)
        return self._cderi_bos

    @property
    def naux(self):
        df = self.mf.with_df
        try:
            return df.auxcell.nao if hasattr(df, "auxcell") else df.auxmol.nao
        except AttributeError:
            return df.get_naoaux()

    @property
    def fock_a(self):
        if self.fock[0].ndim == 1:
            return self.fock
        return self.fock[0]

    @property
    def fock_b(self):
        if self.fock[0].ndim == 1:
            return self.fock
        return self.fock[1]

    @property
    def c_cluster(self):
        if self.cluster.c_active[0].ndim == 1:
            return (self.cluster.c_active, self.cluster.c_active)
        return self.cluster.c_active

    @property
    def overlap(self):
        return self.mf.get_ovlp()

    @property
    def cderi_glob_ov(self):
        """Gets the cderi in the global particle-hole excitation basis, with caching. For use with FoldedSCF mean-fields."""
        if self.kdf is None:
            self.log.warning("Obtaining global CDERI should only be necessary for FoldedSCF calculations.")
        if self._cderi_glob is not None:
            return self._cderi_glob
        c = self.bcluster.forbitals.coeff
        cderi, cderi_neg = self.cderi_glob
        if c[0].ndim == 2:
            noa, nob = self.bcluster.forbitals.nocc
            cderi = (cderi[0][:, :noa, noa:], cderi[1][:, :nob, nob:])
            if cderi_neg is not None:
                cderi_neg = (cderi_neg[0][:, :noa, noa:], cderi_neg[1][:, :nob, nob:])
        else:
            no = self.bcluster.forbitals.nocc
            cderi = cderi[:, :no, no:]
            if cderi_neg is not None:
                cderi_neg = cderi_neg[:, :no, no:]
        return cderi, cderi_neg

    @property
    def cderi_glob(self):
        """Gets the cderi in the global particle-hole excitation basis, with caching. For use with FoldedSCF mean-fields."""
        if self.kdf is None:
            self.log.warning("Obtaining global CDERI should only be necessary for FoldedSCF calculations.")
        if self._cderi_glob is not None:
            return self._cderi_glob
        c = self.bcluster.forbitals.coeff
        if c[0].ndim == 2:
            cderia, cderia_neg = self.mo_cderi_getter((c[0], c[0]))
            cderib, cderib_neg = self.mo_cderi_getter((c[1], c[1]))

            self._cderi_glob = ((cderia, cderib), (cderia_neg, cderib_neg))
        else:
            if self._cderi_glob is None:
                cderi, cderi_neg = self.mo_cderi_getter((c, c))
                self._cderi_glob = (cderi, cderi_neg)
        return self._cderi_glob

    def kernel(self, coupling_exchange=True, freq_exchange=False):
        self.log.info("Generating bosonic interactions")
        self.log.info("-------------------------------")

        freqs, c = self.project_freqs(exchange=freq_exchange)
        couplings = self.project_couplings(exchange=coupling_exchange)
        nonconserving = self.gen_nonconserving(couplings)
        return freqs, tuple([einsum("nm,npq->mpq", c, x) for x in couplings]), c, einsum("n,nm->m", nonconserving, c)

    def project_freqs(self, exchange=False):
        if exchange:
            self.log.warning("Exchange contributions to bosonic frequencies have O(N_clus N^4); use with caution!")
        ca, cb = self.bcluster.coeff_3d_ao

        hbb_fock = (
            einsum("nia,mjb,ab,ij->nm", ca, ca, self.fock_a, self.overlap)
            + einsum("nia,mjb,ab,ij->nm", cb, cb, self.fock_b, self.overlap)
            - einsum("nia,mjb,ij,ab->nm", ca, ca, self.fock_a, self.overlap)
            - einsum("nia,mjb,ij,ab->nm", cb, cb, self.fock_b, self.overlap)
        )

        cderi_bos, cderi_bos_neg = self.cderi_bos
        hbb_coulomb = einsum("Ln,Lm->nm", cderi_bos, cderi_bos)
        hbb_exchange = np.zeros_like(hbb_coulomb)
        if exchange:
            for i, (blk, lab) in enumerate(self._loop_df()):
                sign = 1 if blk is not None else -1
                # hbb_exchange += einsum("Lij,Lab,nia,mjb->nm", lab, lab, ca, ca)
                temp = einsum("Lij,nia->Lnja", lab, ca)
                hbb_exchange -= sign * einsum("Lnja,Lab,mjb->nm", temp, lab, ca)
                temp = einsum("Lij,nia->Lnja", lab, cb)
                hbb_exchange -= sign * einsum("Lnja,Lab,mjb->nm", temp, lab, cb)

        # Want to take eigenvectors of this coupling matrix as our bosonic auxiliaries.
        hbb = hbb_fock + hbb_coulomb + hbb_exchange
        freqs, c = np.linalg.eigh(hbb)
        return freqs, c

    def project_couplings(self, exchange=True):
        """Generate effective bosonic couplings. The two-body component of these is proportional to
            V_npq \propto C_npq <pk||qc>, where C is the bosonic coefficient in the global particle-hole excitation
        basis.
        """

        # For coulombic contributions we just need these cderis.
        cderi_clus, cderi_clus_neg = self.cderi_clus
        cderi_bos, cderi_bos_neg = self.cderi_bos
        couplings_coulomb = [einsum("Ln,Lpq->npq", cderi_bos, x) for x in cderi_clus]

        if cderi_clus_neg[0] is not None:
            if cderi_bos_neg is None:
                raise ValueError("Only have negative cderi contribution via one channel; something's gone wrong.")
            couplings = tuple(
                [orig - einsum("Ln,Lpq->npq", cderi_bos, x) for orig, x in zip(couplings_coulomb, cderi_clus_neg)]
            )

        # Now compute fock contributions.
        def _gen_fock_spinchannel_contrib(rbos, pocc, pvir, c_active, fock):
            # oo (and ai) contrib
            contrib = einsum("njc,ck,jl->nlk", rbos, dot(fock, c_active), dot(self.overlap, c_active))
            # just oo contrib
            contrib -= einsum("nkc,kc,pq->npq", rbos, fock, pocc)
            # just vv contrib
            contrib += einsum("nkc,kc,pq->npq", rbos, fock, pvir)
            # vv (and ai) contrib.
            contrib -= einsum("nka,kb,ac->ncb", rbos, dot(fock, c_active), dot(self.overlap, c_active))
            # NB no ia fock contrib.
            return contrib

        pocc, pvir = self.get_cluster_projectors()

        couplings_fock = [
            _gen_fock_spinchannel_contrib(r, po, pv, c, f)
            for r, po, pv, c, f in zip(
                self.bcluster.coeff_3d_ao, pocc, pvir, self.c_cluster, (self.fock_a, self.fock_b)
            )
        ]

        # Finally, optionally compute exchange contributions.
        couplings_exchange = [np.zeros_like(x) for x in couplings_coulomb]

        c = self.cluster.c_active
        if c[0].ndim == 1:
            ca = cb = c
        else:
            ca, cb = c

        if exchange:
            if self.kdf is not None:
                cderi, cderi_neg = self.cderi_glob
                rbosa, rbosb = self.bcluster.coeff_ex_3d
                if cderi[0].ndim == 3:
                    noa, nob = self.bcluster.forbitals.nocc
                    cderia, cderib = cderi
                    cderi_nega, cderi_negb = (None, None) if cderi_neg is None else cderi_neg
                    ra = dot(ca.T, self.overlap, self.bcluster.forbitals.coeff[0])
                    rb = dot(cb.T, self.overlap, self.bcluster.forbitals.coeff[1])
                else:
                    noa = nob = self.bcluster.forbitals.nocc
                    cderia = cderib = cderi
                    cderi_nega = cderi_negb = cderi_neg
                    ra = rb = dot(c.T, self.overlap, self.bcluster.forbitals.coeff)

                def _gen_exchange_spinchannel_contrib(rbos, cderi, rclus, no):
                    # Want -C_{nkc}<pk|cq> = - C_{nkc} V_{Lpc} V_{Lkq}
                    cderi = einsum("Lpq,rp->Lrq", cderi, rclus)
                    contrib = -einsum("Lpi,Lqa,nia->npq", cderi[:, :, :no], cderi[:, :, no:], rbos)
                    return contrib

                couplings_exchange = [
                    _gen_exchange_spinchannel_contrib(rbosa, cderia, ra, noa),
                    _gen_exchange_spinchannel_contrib(rbosb, cderib, rb, nob),
                ]

                if cderi_nega is not None:
                    couplings_exchange[0] = couplings_exchange[0] - _gen_exchange_spinchannel_contrib(
                        rbosa, cderi_nega, ra, noa
                    )
                    couplings_exchange[1] = couplings_exchange[1] - _gen_exchange_spinchannel_contrib(
                        rbosb, cderi_negb, rb, nob
                    )

            else:
                rbos_a, rbos_b = self.bcluster.coeff_3d_ao

                # Want -C_{nkc}<pk|cq> = - C_{nkc} V_{Lpc} V_{Lkq}
                def _gen_exchange_spinchannel_contrib(l, c, rbos):
                    la_loc = np.tensordot(l, c, axes=(2, 0))  # "Lab,bp->Lap"
                    temp = einsum("Lkp,Lcq->kcpq", la_loc, la_loc)
                    contrib = -einsum("kcpq,nkc->npq", temp, rbos)
                    return contrib

                for i, (blk, lab) in enumerate(self._loop_df()):
                    if blk is not None:
                        couplings_exchange[0] = couplings_exchange[0] + _gen_exchange_spinchannel_contrib(
                            lab, ca, rbos_a
                        )
                        couplings_exchange[1] = couplings_exchange[1] + _gen_exchange_spinchannel_contrib(
                            lab, cb, rbos_b
                        )
                    else:
                        couplings_exchange[0] = couplings_exchange[0] - _gen_exchange_spinchannel_contrib(
                            lab, ca, rbos_a
                        )
                        couplings_exchange[1] = couplings_exchange[1] - _gen_exchange_spinchannel_contrib(
                            lab, cb, rbos_b
                        )

        couplings = [x + y + z for x, y, z in zip(couplings_coulomb, couplings_exchange, couplings_fock)]
        return couplings

    def gen_nonconserving(self, couplings):
        """Generate the particle number non-conserving part of the bosonic Hamiltonian."""
        rbos_a, rbos_b = self.cluster.bosons.coeff_3d_ao
        pocc = self.get_cluster_projectors()[0]
        # This is the normal-ordered contribution, arising from non-canonical HF references.
        contrib = einsum("npq,pq->n", rbos_a, self.fock_a) + einsum("npq,pq->n", rbos_b, self.fock_b)
        # This arises from the transformation of the occupied term out of normal ordering
        contrib -= einsum("npq,pq->n", couplings[0], pocc[0]) + einsum("npp,pp->n", couplings[1], pocc[1])
        return contrib

    def get_cluster_projectors(self):
        """Get the projectors in the cluster basis into the occupied and virtual subspaces."""

        def _get_cluster_spinchannel(c, co, cv):
            so = dot(c.T, self.overlap, co)
            po = dot(so, so.T)
            sv = dot(c.T, self.overlap, cv)
            pv = dot(sv, sv.T)
            return po, pv

        if self.cluster.c_active_occ[0].ndim == 1:
            poa, pva = _get_cluster_spinchannel(
                self.cluster.c_active, self.cluster.c_active_occ, self.cluster.c_active_vir
            )
            pob, pvb = poa, pva
        else:
            poa, pva = _get_cluster_spinchannel(
                self.cluster.c_active[0], self.cluster.c_active_occ[0], self.cluster.c_active_vir[0]
            )
            pob, pvb = _get_cluster_spinchannel(
                self.cluster.c_active[1], self.cluster.c_active_occ[1], self.cluster.c_active_vir[1]
            )
        return (poa, pob), (pva, pvb)

    def _loop_df(self, blksize=None):
        nao = self.mf.mol.nao
        df = self.mf.with_df
        naux = self.naux
        if blksize is None:
            blksize = int(1e9 / naux * nao * nao * 8)
        # PBC:
        if hasattr(df, "sr_loop"):
            blk0 = 0
            for labr, labi, sign in df.sr_loop(compact=False, blksize=blksize):
                assert np.allclose(labi, 0)
                assert np.allclose(labi, 0)
                labr = labr.reshape(-1, nao, nao)
                if sign == 1:
                    blk1 = blk0 + labr.shape[0]
                    blk = np.s_[blk0:blk1]
                    blk0 = blk1
                    yield blk, labr
                elif sign == -1:
                    yield None, labr
        # No PBC:
        blk0 = 0
        for lab in df.loop(blksize=blksize):
            blk1 = blk0 + lab.shape[0]
            blk = np.s_[blk0:blk1]
            blk0 = blk1
            lab = pyscf.lib.unpack_tril(lab)
            yield blk, lab
