# Public methods commitment and prospective registration

The release is committed and tagged as `methods-2026-11-02` in the public [BenchedMark methods repository](https://github.com/spencer-c-reed/benchedmark-methods). Both the commit and tag are cryptographically signed. The [GitHub release](https://github.com/spencer-c-reed/benchedmark-methods/releases/tag/methods-2026-11-02) supplies a third-party publication timestamp (`created_at` / `published_at` in the public GitHub release API). Git commit author/committer dates alone are self-reported and are not independent proof of an earlier freeze.

`SHA256SUMS` records the SHA-256 of every payload file, including `ANALYSIS-PLAN.md`; it excludes itself to avoid a circular hash. The signed commit tree binds this manifest too. The public SSH signing key and `allowed_signers` file support local signature checks. This is a release-specific bot key; it does not establish a personal identity or constitute an OSF registration. OSF is preferred for a supplementary filing if bot signup is available. No OSF filing is asserted here.

```sh
git clone https://github.com/spencer-c-reed/benchedmark-methods.git
cd benchedmark-methods
git -c gpg.ssh.allowedSignersFile="$PWD/allowed_signers" verify-commit HEAD
git -c gpg.ssh.allowedSignersFile="$PWD/allowed_signers" verify-tag methods-2026-11-02
sha256sum -c SHA256SUMS
```

The pilot and some scoring/diagnostics predate this public commitment. **It is prospective only for analyses not yet run.** No public timestamp before the pilot, completed empirical study or retroactive confirmatory status is claimed. The analysis plan names requirements and remaining limitations; internal dated notes are historical context, not third-party proof of their original timing. Future amendments require a new immutable version and must disclose whether relevant outcomes had already been examined.
