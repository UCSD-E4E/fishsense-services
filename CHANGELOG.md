# Changelog

## [0.4.0](https://github.com/UCSD-E4E/fishsense-services/compare/v0.3.2...v0.4.0) (2026-10-08)


### Features

* **nas:** synology-filestation 0.10.0; a fallback to FileStation is loud ([e396525](https://github.com/UCSD-E4E/fishsense-services/commit/e39652557abcabc0d956a22d43f035808e98c5ee))
* **ops:** repair captures whose files moved on the NAS ([6a563f5](https://github.com/UCSD-E4E/fishsense-services/commit/6a563f56b757414c66d31cd8a08e208ad30d46eb))


### Bug Fixes

* **nrp:** a wake protects a processor only until its work reaches the queue ([788d085](https://github.com/UCSD-E4E/fishsense-services/commit/788d0852e79172884cac2c5cb5c28cf78a307fd1))
* **slate-detect:** buffer one firing instead of skipping it ([b8c26c6](https://github.com/UCSD-E4E/fishsense-services/commit/b8c26c6e38419b47b3ef2a46e2fb9e9bb98baf31))

## [0.3.2](https://github.com/UCSD-E4E/fishsense-services/compare/v0.3.1...v0.3.2) (2026-10-07)


### Bug Fixes

* **nas:** sign in to SMB on the KRG domain, and take synology-filestation 0.9.0 ([69ec8c7](https://github.com/UCSD-E4E/fishsense-services/commit/69ec8c7c0bdbd5abd6baefe332435069bac14fa7))
* **nas:** sign in to SMB on the KRG domain, and take synology-filestation 0.9.0 ([51bf2a1](https://github.com/UCSD-E4E/fishsense-services/commit/51bf2a16c7645a4a4479c96756e3296a69194787))

## [0.3.1](https://github.com/UCSD-E4E/fishsense-services/compare/v0.3.0...v0.3.1) (2026-10-06)


### Bug Fixes

* **slate-detect:** a raw moved on the NAS no longer stalls the backlog ([1a4ea2e](https://github.com/UCSD-E4E/fishsense-services/commit/1a4ea2ea5df49eb86fc037e944bebd01c162fff3))
* **slate-detect:** a raw moved on the NAS no longer stalls the backlog ([0278a1e](https://github.com/UCSD-E4E/fishsense-services/commit/0278a1e8a22455d1087633df13ee51f462e98467))

## [0.3.0](https://github.com/UCSD-E4E/fishsense-services/compare/v0.2.0...v0.3.0) (2026-10-06)


### Features

* **slate-detect:** one run drains the backlog, not one dive ([30b46bc](https://github.com/UCSD-E4E/fishsense-services/commit/30b46bcb58915e15055f0b427ce27733424bc9d0))
* **slate-detect:** one run drains the backlog, not one dive ([f73513b](https://github.com/UCSD-E4E/fishsense-services/commit/f73513be7be710e6fbdebe62f8010efeb7380e90))

## [0.2.0](https://github.com/UCSD-E4E/fishsense-services/compare/v0.1.2...v0.2.0) (2026-10-06)


### Features

* **api:** automatic results as their own append-only track ([d3547e1](https://github.com/UCSD-E4E/fishsense-services/commit/d3547e13eb7a3315bb8f2c30d5617312c540619d))
* **api:** slate presence predictions, and detector frames feed stage 9 ([767a845](https://github.com/UCSD-E4E/fishsense-services/commit/767a8450b4c637d04e241c0b7b1a7f6cd208006f))
* **api:** validate-automatic, the paper's metrics on a database ([c9341da](https://github.com/UCSD-E4E/fishsense-services/commit/c9341da9c48e8fdcbd8b40b1272d7f168c17b8f6))
* **contracts:** slate presence detector contract ([10e1597](https://github.com/UCSD-E4E/fishsense-services/commit/10e1597ab489bf4b58439745e35923bda9306ca8))
* **deploy:** score every frame with the slate detector in production ([65f4152](https://github.com/UCSD-E4E/fishsense-services/commit/65f41522883af7bc7502745a6e411271e02f373c))
* **orchestrator:** automatic results stage, off by default ([53cd02f](https://github.com/UCSD-E4E/fishsense-services/commit/53cd02fc4a1919bd6a01b72ff18f7ccc62d2c2d6))
* **orchestrator:** slate detection stage, shipped disabled; detector frames carry provenance ([885ce53](https://github.com/UCSD-E4E/fishsense-services/commit/885ce53940b93b07b0bb8ce85e1db86e9e4c83e4))
* **processor:** automatic frames stage (dot, SAM 3.1 at the dot, head/tail) ([099bd17](https://github.com/UCSD-E4E/fishsense-services/commit/099bd1796cebf79e040d1fb6c06bb45739837886))
* **processor:** label-free calibration and automatic length stages ([e1b14a9](https://github.com/UCSD-E4E/fishsense-services/commit/e1b14a98319bb46706a01af0d15f8e269162f140))
* **processor:** label-free size-constancy laser calibration, ported from cscw ([8cd8ec5](https://github.com/UCSD-E4E/fishsense-services/commit/8cd8ec5405f4825b90eb5cd8a3edd14743910ea5))
* publication-grade slate predictions and their evaluation view ([ccb95e8](https://github.com/UCSD-E4E/fishsense-services/commit/ccb95e8fa2785e863f8ece3a21406340c3d773c5))
* slate presence detector and the automatic results track (off by default) ([933e3ca](https://github.com/UCSD-E4E/fishsense-services/commit/933e3ca594e249bb2b1221c056a7ce2db0dec55e))


### Bug Fixes

* **api:** a frame the slate detector names is not measured; harness skips unrun frames ([f7afa1d](https://github.com/UCSD-E4E/fishsense-services/commit/f7afa1d511661f4d602ef69139c72f755fba9547))
* **automatic-results:** the cohort reads the slate detector, and a corrupt raw is recorded ([3b7ecb5](https://github.com/UCSD-E4E/fishsense-services/commit/3b7ecb587a71fbf475b57740b88247ed0e14c04c))


### Documentation

* platform admin (decided, deferred) and the cutover as run ([1aa396e](https://github.com/UCSD-E4E/fishsense-services/commit/1aa396e20fa4f5a7b24f6ad4724528bf8e41166c))

## [0.1.2](https://github.com/UCSD-E4E/fishsense-services/compare/v0.1.1...v0.1.2) (2026-10-06)


### Bug Fixes

* **deploy:** read services_db from the platform-generated path ([b407871](https://github.com/UCSD-E4E/fishsense-services/commit/b407871f2a8cc55fda7b16d8a3d1c664194f1198))
* **deploy:** read services_db from the platform-generated path ([10b4c9a](https://github.com/UCSD-E4E/fishsense-services/commit/10b4c9a9ffd9697bcc20946b9ae9c81dbeee43e8))
* **orchestrator:** a blank NRP kubeconfig means no NRP; the slate sync has a cursor store ([a17895b](https://github.com/UCSD-E4E/fishsense-services/commit/a17895b62ab973df8722a123f98757360e8d8ffd))
* **orchestrator:** a blank NRP kubeconfig means no NRP; the slate sync has a cursor store ([df174e6](https://github.com/UCSD-E4E/fishsense-services/commit/df174e65b1154ff515b38bb772a2e48f16d3f02a))


### Documentation

* **cutover:** align with krg-infra's merged hand-off ([#549](https://github.com/UCSD-E4E/fishsense-services/issues/549)–[#551](https://github.com/UCSD-E4E/fishsense-services/issues/551)) ([37fb514](https://github.com/UCSD-E4E/fishsense-services/commit/37fb51485fa1c0c9e92d55dfa70b11b50091cb39))

## [0.1.1](https://github.com/UCSD-E4E/fishsense-services/compare/v0.1.0...v0.1.1) (2026-10-02)


### Bug Fixes

* **deploy:** v2's workers read the platform's root-only renders; act on krg-infra's review ([ecb8891](https://github.com/UCSD-E4E/fishsense-services/commit/ecb8891c3fed199545301b29044f70936d83d831))
* **deploy:** v2's workers read the platform's root-only renders; act on krg-infra's review ([6ca1c48](https://github.com/UCSD-E4E/fishsense-services/commit/6ca1c48ae7585c0367543509e616f3b08e725232))


### Documentation

* **cutover:** two clean rehearsals on last night's dump with v0.1.0 ([5650dc9](https://github.com/UCSD-E4E/fishsense-services/commit/5650dc921d2a180a1e3e294a440af93f9669512b))
* **cutover:** two clean rehearsals on last night's dump with v0.1.0 ([178cf74](https://github.com/UCSD-E4E/fishsense-services/commit/178cf7443555c27d1f2b0edb4ae431d615de0396))
* dive 509 was fixed in v1 on 2026-09-16 ([08a3903](https://github.com/UCSD-E4E/fishsense-services/commit/08a3903a9ecc8099fe4b23ebe78e318083a58312))
* revive [#932](https://github.com/UCSD-E4E/fishsense-services/issues/932)'s eroded laser labels in v2, after cutover ([3b9dbe4](https://github.com/UCSD-E4E/fishsense-services/commit/3b9dbe488b31140bdf526787586bfa824f03609d))

## 0.1.0 (2026-10-01)


### Features

* **api:** an admin asks for a dive's frames to be redrawn, per label kind ([0e2c645](https://github.com/UCSD-E4E/fishsense-services/commit/0e2c6456e767a99e41070460e67d6da5e30a92cb))
* **api:** an admin clears a calibration refusal and sets or clears a dive's calibration target ([dd247b9](https://github.com/UCSD-E4E/fishsense-services/commit/dd247b9608440e5b5dccb62bba274a92b4f39f47))
* **api:** audit that tenant-to-tenant references carry tenant_id ([31e94f2](https://github.com/UCSD-E4E/fishsense-services/commit/31e94f21245cea69e03cff96e0766f908c626e63))
* **api:** audit that views run as their caller, never their owner ([63a7cfb](https://github.com/UCSD-E4E/fishsense-services/commit/63a7cfb91f60b6a8621d42e9e4d00046a53420e7))
* **api:** audit-range-trend, v1's range-trend calibration audit CLI ([53c398d](https://github.com/UCSD-E4E/fishsense-services/commit/53c398d7ca0d68162f8e71b0e39283fea6370f38))
* **api:** boot from the environment under uvicorn ([024e74f](https://github.com/UCSD-E4E/fishsense-services/commit/024e74ff234e99f58f8a2dd4748bcc9790f65ceb))
* **api:** camera calibrations -- per device, append-only ([2279960](https://github.com/UCSD-E4E/fishsense-services/commit/2279960720165f6eb73ac1bd2e1ff36579cf31d4))
* **api:** carry v1's laser provenance: superseded reasons, noise estimators ([f6e78ce](https://github.com/UCSD-E4E/fishsense-services/commit/f6e78ce141f1f01de81de3ac828f52e5cc636e0e))
* **api:** create the caller's user row on first valid login ([40c738d](https://github.com/UCSD-E4E/fishsense-services/commit/40c738d3b7a9a00062d4f9a8268b1ff9b460b12a))
* **api:** dive laser lines -- the within-dive dot fit, append-only ([be95b80](https://github.com/UCSD-E4E/fishsense-services/commit/be95b80dc0dd35869b40321c2ae0b27bd24709f5))
* **api:** dive_pipeline_status, v1's shape over v2's cohorts ([0dd6098](https://github.com/UCSD-E4E/fishsense-services/commit/0dd60984f44370523fafe4a8595aab0ea25d7f67))
* **api:** dives and captures, with same-tenant references in the database ([c16e526](https://github.com/UCSD-E4E/fishsense-services/commit/c16e526e7463e75ce36c2824f5f6fed3f4746966))
* **api:** fetch signing keys from Authentik's JWKS ([917899b](https://github.com/UCSD-E4E/fishsense-services/commit/917899b13c4b0852be30a9cc9065978cf32be241))
* **api:** find a dive by its number for the lattice study ([7c734f2](https://github.com/UCSD-E4E/fishsense-services/commit/7c734f200db6d2c3431ed9b381c81ed935615736))
* **api:** first route, /tenants/{slug}/devices, end to end ([2278f62](https://github.com/UCSD-E4E/fishsense-services/commit/2278f625fa12abfa336483ac1cf4f021600b1d27))
* **api:** fish-model identity, fish, and dive frame clusters ([1440feb](https://github.com/UCSD-E4E/fishsense-services/commit/1440febec8b121a434e4f266d29e8063f99c3f04))
* **api:** fishsense_analytics, a read role bound to the lab tenant by RLS ([f5f054f](https://github.com/UCSD-E4E/fishsense-services/commit/f5f054fab71fcf87ce68f8a5d2a9051c9fca3876))
* **api:** global reference data -- species, targets, fish models, slates ([40b3818](https://github.com/UCSD-E4E/fishsense-services/commit/40b3818af96c0e3c08457223d0ba9b46973e2baa))
* **api:** head/tail predictions accept v1's decode_failed abstention ([bea220f](https://github.com/UCSD-E4E/fishsense-services/commit/bea220fc73c08cbc205168ca24d5e230fd41894a))
* **api:** integer numbers for every row v1 had an id for ([1523646](https://github.com/UCSD-E4E/fishsense-services/commit/1523646a8f1607879741b49100418e33dbe55554))
* **api:** Label Studio labels and sync cursors ([327b883](https://github.com/UCSD-E4E/fishsense-services/commit/327b883117dfd7dcfec580ec571df873540aa17d))
* **api:** labels, predictions, fish, clusters, depths and measurements ([da4375b](https://github.com/UCSD-E4E/fishsense-services/commit/da4375baacf8f5bebafd979d75199f25f8ebcf9a))
* **api:** laser calibrations -- per dive, append-only, refusals as rows ([d1f6c6b](https://github.com/UCSD-E4E/fishsense-services/commit/d1f6c6b72e01dc15a8e47cf7aa2a1a6c8dd3b556))
* **api:** laser depths and measurements, with "current" per 9.13 ([38991eb](https://github.com/UCSD-E4E/fishsense-services/commit/38991eb00d787be6e2a391d923266e9f4483a91d))
* **api:** laser-depth and stage-14 stores, refusals and the binding rule ([833d8ba](https://github.com/UCSD-E4E/fishsense-services/commit/833d8ba93912bf132962350c5c599ac30066d195))
* **api:** look up and record a dive's Label Studio project ([d877f07](https://github.com/UCSD-E4E/fishsense-services/commit/d877f07e15b22b737c94f9a0c3c61d972af3ed68))
* **api:** migrate as a separate command, with the owner's DSN ([81aee92](https://github.com/UCSD-E4E/fishsense-services/commit/81aee92446a24b53bc67089843f45677291190e5))
* **api:** migrate calibrations from v1 (cycle 2) ([47a1d55](https://github.com/UCSD-E4E/fishsense-services/commit/47a1d55468f623e45e8115d2b9967db7f5611e5d))
* **api:** migrate depths and measurements from v1 (cycle 6) ([5b668b9](https://github.com/UCSD-E4E/fishsense-services/commit/5b668b97e9327ff87056a35758fafc9aac9f0d44))
* **api:** migrate fish and frame clusters from v1 (cycle 5) ([157ec0b](https://github.com/UCSD-E4E/fishsense-services/commit/157ec0b2f59c662a753ad6f06028241ef494c888))
* **api:** migrate labels and sync cursors from v1 (cycle 3) ([dce290d](https://github.com/UCSD-E4E/fishsense-services/commit/dce290d0802c78cdc3cd5b430408d3fe5852f833))
* **api:** migrate predictions from v1 (cycle 4) ([1af3aa0](https://github.com/UCSD-E4E/fishsense-services/commit/1af3aa0a561381e760ef39d0a8a33b698eca7d57))
* **api:** migrate reports the revision it reached ([f023a2d](https://github.com/UCSD-E4E/fishsense-services/commit/f023a2d3ae34f8eaff28972a0a132e11887d8f1d))
* **api:** migrate-v1 command with a go/no-go validation (cycle 7) ([ad2d8e2](https://github.com/UCSD-E4E/fishsense-services/commit/ad2d8e2088a2b1a9982ea2f60951030b3b01d0f4))
* **api:** model predictions -- laser dot, slate, head/tail ([8b7fb7b](https://github.com/UCSD-E4E/fishsense-services/commit/8b7fb7b96dcdb7e972976d8240761c4137345695))
* **api:** port v1's scale-free range-trend calibration audit ([04cde74](https://github.com/UCSD-E4E/fishsense-services/commit/04cde74837662b53b0f51d8e5d8ebfcc345518e8))
* **api:** publish the OpenAPI document the web generates its client from ([6f58cc5](https://github.com/UCSD-E4E/fishsense-services/commit/6f58cc5151cc1e835600931eb8c8d28516d905dc))
* **api:** range_trend_inputs reads a dive's range-trend audit inputs ([7e40d6c](https://github.com/UCSD-E4E/fishsense-services/commit/7e40d6c00692cd57da295b3661db5f99862a13da))
* **api:** record the target a laser calibration used; append refusal clears ([47e3907](https://github.com/UCSD-E4E/fishsense-services/commit/47e3907594025f371ef327ade4d29840fc62d78f))
* **api:** record which Label Studio project holds which dive's labels ([5158882](https://github.com/UCSD-E4E/fishsense-services/commit/515888201fff00086c44722d5429a00281167c98))
* **api:** schema-wide tenancy audit, enforced by migrate ([5caa787](https://github.com/UCSD-E4E/fishsense-services/commit/5caa787f5f25787fec2573f19badc354d8dc4a20))
* **api:** slate and laser-calibration stores ([b97342e](https://github.com/UCSD-E4E/fishsense-services/commit/b97342e7b0ddd1455246c9fb021d670db70a3341))
* **api:** species_predictions and the head/tail mask box ([e83879a](https://github.com/UCSD-E4E/fishsense-services/commit/e83879a4971a1301e735d207e5f727594a5355ec))
* **api:** tenancy foundation, packaging, and the first domain tables ([5878d34](https://github.com/UCSD-E4E/fishsense-services/commit/5878d34d8e122112ceb9252719f219e20b408d66))
* **api:** tenant row-level security as the database backstop ([806ee31](https://github.com/UCSD-E4E/fishsense-services/commit/806ee31e748ab21a7e98c14826862b9992da5b5b))
* **api:** test tiers, e2e suite, audit rules, and calibration tables ([cb9b926](https://github.com/UCSD-E4E/fishsense-services/commit/cb9b926fc706fd44269eaef2dd325205e06e37a6))
* **api:** the database side of stage 1, as the orchestrator's principal ([4f3b791](https://github.com/UCSD-E4E/fishsense-services/commit/4f3b7912082944d8518c4e20ae417fecf71cfd29))
* **api:** the database side of the Label Studio label sync ([524155d](https://github.com/UCSD-E4E/fishsense-services/commit/524155df6b916a617ed9042767e86dd0d629e1be))
* **api:** the head/tail label sync writes only the columns it owns ([cc512e1](https://github.com/UCSD-E4E/fishsense-services/commit/cc512e16d6f0eeef52c4447b17ce4751ef4074a6))
* **api:** the head/tail stages' database side, tenant-scoped ([b140078](https://github.com/UCSD-E4E/fishsense-services/commit/b140078680fde8d02ff8900e082bcac8e5261928))
* **api:** the laser slice's store -- cohorts, gate verdicts, populate rows, validation writes ([893fcf2](https://github.com/UCSD-E4E/fishsense-services/commit/893fcf26e03b263f448417882ebd341a8e14d81b))
* **api:** the SQL forms of the taxonomy predicates, checked on Postgres ([40c2ce7](https://github.com/UCSD-E4E/fishsense-services/commit/40c2ce712b421c0e4ee9a93e1e3ccd871b3e9b4d))
* **api:** the tenant-scoped database side of dive ingest ([66047bf](https://github.com/UCSD-E4E/fishsense-services/commit/66047bf3d5f783b3a50bf4bc001466fd75e1b9f9))
* **api:** the web portal's routes, tenant-scoped, with an admin role for its edits ([bb1bbf3](https://github.com/UCSD-E4E/fishsense-services/commit/bb1bbf348d291484f9cf35d1d22fb85d3da0f4fd))
* **api:** typed SQLAlchemy 2.0 models, pinned to the migrations ([5f6e19f](https://github.com/UCSD-E4E/fishsense-services/commit/5f6e19fcd234ab42c7149990d7975f601239d29d))
* **api:** users, memberships, and caller-scoped membership resolution ([565e51c](https://github.com/UCSD-E4E/fishsense-services/commit/565e51c596f6129c911f3d1efc49d5a409bfdf49))
* **api:** v1 -&gt; v2 data migration with go/no-go, rehearsed on production data ([e134c97](https://github.com/UCSD-E4E/fishsense-services/commit/e134c977ecc7896520f8e426e2ddb3a8a71ad0ae))
* **api:** v1 -&gt; v2 migration job, first cycle (reference data to captures) ([6f51a20](https://github.com/UCSD-E4E/fishsense-services/commit/6f51a2074e7306125833f30c7ef40cfaadddf356))
* **api:** v1-shaped research views, v1's fish views and a lab-bound research role ([ba8ade5](https://github.com/UCSD-E4E/fishsense-services/commit/ba8ade53442aeefa7da2ec70f4b05b761cfdddab))
* **api:** validate Authentik access tokens in-app ([b359f71](https://github.com/UCSD-E4E/fishsense-services/commit/b359f71a0c8b03869039e262b9a4dfd0a30cc09f))
* **api:** which captures a dive stages, and which scratch its cleanup may delete ([26bb322](https://github.com/UCSD-E4E/fishsense-services/commit/26bb322da17e3c06ea8be93d1b8cd1e047435f83))
* **contracts:** BioCLIP species prediction contract and head/tail mask_bbox (v5) ([da67068](https://github.com/UCSD-E4E/fishsense-services/commit/da67068663d29fb2de5671019f68b0588064c621))
* **contracts:** laser depth and measure-fish DTOs, with refusals ([11e9494](https://github.com/UCSD-E4E/fishsense-services/commit/11e949469505b1d3bda6b15156a7222f2aead5de))
* **contracts:** the content_of_image taxonomy, ported from v1 ([b0ea2ce](https://github.com/UCSD-E4E/fishsense-services/commit/b0ea2ce4428bb1f5b4a11b0406594c3f95106c9e))
* **contracts:** the head/tail stages' DTOs, predictor version and Label Studio tag ([acea148](https://github.com/UCSD-E4E/fishsense-services/commit/acea1484e328d84583b28a6ab8547fd842ca1874))
* **contracts:** the laser slice's contract -- region, stage version, gate budget, rows-in payloads ([c68181a](https://github.com/UCSD-E4E/fishsense-services/commit/c68181a865f2270229c09bf2d38037c53fcced9f))
* **contracts:** the object store's connection and the ObjectRef the processor is handed ([3d60907](https://github.com/UCSD-E4E/fishsense-services/commit/3d60907cdc46df097b52d1e79d486db8accbe807))
* **contracts:** the versioned processing contract package ([bb830d4](https://github.com/UCSD-E4E/fishsense-services/commit/bb830d4428f15d98f22426e0745f54bb3a3bd7ff))
* **contracts:** v2 carries ObjectRef; the port plan says how to use the foundations ([665f594](https://github.com/UCSD-E4E/fishsense-services/commit/665f5940e416efa8813f676414fb71b63dbde5ba))
* **deploy:** Superset's pipeline datasets, run against the view in a test ([6d799b8](https://github.com/UCSD-E4E/fishsense-services/commit/6d799b82120bf14cf1200bd9c1b02ecfcfc67064))
* **deploy:** the orchestrator's image and compose service ([60e3a82](https://github.com/UCSD-E4E/fishsense-services/commit/60e3a825cb90ad42d2212978e5cb92a02dd81f76))
* **deploy:** the production interior for the fishsense Incus slot ([512c499](https://github.com/UCSD-E4E/fishsense-services/commit/512c4995e71cc3c73f30da058d0a0a75e454229e))
* **ingest:** the catalog preflight asks, as the orchestrator's principal ([5b4db49](https://github.com/UCSD-E4E/fishsense-services/commit/5b4db49861abeb5379b949da98a30775ac1cdd8a))
* initial commit ([7a8ef96](https://github.com/UCSD-E4E/fishsense-services/commit/7a8ef960f523ef5e272a0d0e251996a9ce8fea31))
* **ops:** checksum verification, one dive and the sweep, read-only ([c2f9768](https://github.com/UCSD-E4E/fishsense-services/commit/c2f9768facc51a71ab033ad7008e18091b5cf4f7))
* **ops:** forward the rotated Temporal leaf to the processor's NRP Secret ([7eb0753](https://github.com/UCSD-E4E/fishsense-services/commit/7eb0753ee03303dc51b23217c40fda92d86eba92))
* **ops:** the hourly labeling-config reconcile, and ops as a stage ([da30195](https://github.com/UCSD-E4E/fishsense-services/commit/da30195bac3730ed2edc17d008a89a5db6021a25))
* **ops:** the nightly backup, pg_dump to the NAS as its own process ([8bb5739](https://github.com/UCSD-E4E/fishsense-services/commit/8bb57398c4c192e45ad077741a825d5dcedb41fb))
* **orchestrator:** BioCLIP species pre-annotation stage, shipped disabled ([ddefa83](https://github.com/UCSD-E4E/fishsense-services/commit/ddefa83c80170560b4b5f19ab10af65dd3ec7c63))
* **orchestrator:** Label Studio predictions in the one adapter ([810d2f2](https://github.com/UCSD-E4E/fishsense-services/commit/810d2f2ee71089e11b5254a3b8aaddfe23443d05))
* **orchestrator:** laser-depth and measure-fish parents on v1's schedules ([86f44e2](https://github.com/UCSD-E4E/fishsense-services/commit/86f44e2993bb7c04e3c0219f6cf95c0d5d9f7d51))
* **orchestrator:** port create and finalize, the commit protocol ([226eac8](https://github.com/UCSD-E4E/fishsense-services/commit/226eac88939444f9f8ab1d0d0482fd82d0575335))
* **orchestrator:** port IngestDiveWorkflow ([232e2a7](https://github.com/UCSD-E4E/fishsense-services/commit/232e2a73ce69e168dce136770976a4a724a11a50))
* **orchestrator:** port scan-and-register ([41bdb76](https://github.com/UCSD-E4E/fishsense-services/commit/41bdb765b9bf204f8a04fef3c950816c6506bfc9))
* **orchestrator:** port stage 1's selector, parent workflow and schedule ([417676c](https://github.com/UCSD-E4E/fishsense-services/commit/417676ce1133c6d60c34e9df215cdb655bf2b65a))
* **orchestrator:** port the ingest contracts, adapted for tenancy ([ce0163f](https://github.com/UCSD-E4E/fishsense-services/commit/ce0163f24fee6770144ad0b28310507dd9265645))
* **orchestrator:** port the Label Studio laser-label sync ([2b80224](https://github.com/UCSD-E4E/fishsense-services/commit/2b802240d2a15ba869a65c2a974e410a1750f062))
* **orchestrator:** port the list-dive-folder activity ([5813d28](https://github.com/UCSD-E4E/fishsense-services/commit/5813d28d2b6a0db7385463d7bfa2730b834a0a93))
* **orchestrator:** port the NAS client, error classifier and frame conventions ([5fcb447](https://github.com/UCSD-E4E/fishsense-services/commit/5fcb4478adad659e80f085a6a854175ac03eb280))
* **orchestrator:** port the preflight activity ([7e5720f](https://github.com/UCSD-E4E/fishsense-services/commit/7e5720f3dee9c5714d3ab8e587c8b0514fd80157))
* **orchestrator:** port v1 dive ingest ([8d4ba9d](https://github.com/UCSD-E4E/fishsense-services/commit/8d4ba9d5db03b0b019fcbfe57599e998870db1e2))
* **orchestrator:** scaffold the v2 orchestrator; port v1's EXIF reader ([f620f04](https://github.com/UCSD-E4E/fishsense-services/commit/f620f04b8bdac82cd9855e6e7f20888dcc9677d2))
* **orchestrator:** stage 13, checkerboard calibration and the lattice study ([3f853bc](https://github.com/UCSD-E4E/fishsense-services/commit/3f853bc1fcf290fdb2fcf9076f23529da61f1b35))
* **orchestrator:** stage 9, the dive-slate project and its sync ([116b49a](https://github.com/UCSD-E4E/fishsense-services/commit/116b49a94d6f4a9503b1df99a27b70765cf02d1e))
* **orchestrator:** stage a dive's raw frames, clean them up, and find a processed JPEG ([28b0b33](https://github.com/UCSD-E4E/fishsense-services/commit/28b0b339512dc2a2d869addc0c33982905450546))
* **orchestrator:** stand the processor up on NRP and tear it down when idle ([c522748](https://github.com/UCSD-E4E/fishsense-services/commit/c522748b45f8dccb3e10229625113bbfc58fb9ad))
* **orchestrator:** the head/tail activities -- cohorts, resolvers, populate, backfill, sync ([553287f](https://github.com/UCSD-E4E/fishsense-services/commit/553287f1331b0341ad12ff3fc9705c60609f224f))
* **orchestrator:** the head/tail workflows, stage and v1's schedules ([4b9bcca](https://github.com/UCSD-E4E/fishsense-services/commit/4b9bcca55388516c3186f8c57e33f641852b71df))
* **orchestrator:** the Label Studio write side every populate shares ([27f8917](https://github.com/UCSD-E4E/fishsense-services/commit/27f8917b834eaf671d3797baf3c40f019fa4980c))
* **orchestrator:** the laser slice -- stage 0.1, prediction, the gate, populate, validation, remediation ([b07db45](https://github.com/UCSD-E4E/fishsense-services/commit/b07db459a068ed27bd9f37da8076afee197a97cd))
* **orchestrator:** the object store as a stage, wired end to end ([322885c](https://github.com/UCSD-E4E/fishsense-services/commit/322885cc25226bd26cbb69524066e8352862d3e6))
* **orchestrator:** the post-converge smoke test, GO or NO-GO (PLAN §6.6 step 6) ([161529b](https://github.com/UCSD-E4E/fishsense-services/commit/161529ba9e6d8c84253e5f5abddd25f7b6ebc45d))
* **orchestrator:** the tenant key layout, v1's JPEG keys kept readable, and the staging client ([ce686e4](https://github.com/UCSD-E4E/fishsense-services/commit/ce686e4c41cff4a68b7ea1840157605390817d5d))
* **orchestrator:** the worker entrypoint ([217f238](https://github.com/UCSD-E4E/fishsense-services/commit/217f238f882dd7d60ec65f5efd0fa79c3dc0e10f))
* **orchestrator:** wake the light processor before stage 1 dispatches ([bab5c11](https://github.com/UCSD-E4E/fishsense-services/commit/bab5c11348ec23752bc8ea03a71018528a65a1d2))
* port the Label Studio laser-label sync ([cdd0fca](https://github.com/UCSD-E4E/fishsense-services/commit/cdd0fca05d312485013402dd052ac2c93edbe684))
* **processor:** BioCLIP species prediction stage; head/tail records its mask box ([7deab67](https://github.com/UCSD-E4E/fishsense-services/commit/7deab67136241b1e7bfacab1efebfec3217b6e95))
* **processor:** head/tail prediction, SAM 3.1 on the GPU role with the Mask R-CNN fallback ([ce9e0b0](https://github.com/UCSD-E4E/fishsense-services/commit/ce9e0b0287a20c4c6090f2349684bcead81d1c0f))
* **processor:** laser calibration kernels, gates and checkerboard detection ([fac8d4e](https://github.com/UCSD-E4E/fishsense-services/commit/fac8d4e6eed174de527888752a414cdc083b856f))
* **processor:** laser depth and measure-fish geometry on the light role ([ea3c777](https://github.com/UCSD-E4E/fishsense-services/commit/ea3c7773a4611325be7800e17eef70ccc6eb461d))
* **processor:** laser-label judgement, the auto-accept gate and remediation planning on the light role ([c7d14d6](https://github.com/UCSD-E4E/fishsense-services/commit/c7d14d6a195ec5329834c619e6828e8ff5f71a1c))
* **processor:** model weights from Garage, verified by fishsense-core ([a647551](https://github.com/UCSD-E4E/fishsense-services/commit/a6475512b62eef56b6e682a922198a97fc800866))
* **processor:** port stage 1's clustering kernel and workflow ([9eaaca9](https://github.com/UCSD-E4E/fishsense-services/commit/9eaaca92645d0ec2f2760d7886e109b6572c57e6))
* **processor:** read staged frames and write a tenant's JPEGs by the ref it is handed ([e151cb7](https://github.com/UCSD-E4E/fishsense-services/commit/e151cb71fccc76236424b3b9854226b317ecea90))
* **processor:** serve one role, chosen by FISHSENSE_PROCESSOR_ROLE ([dbfc064](https://github.com/UCSD-E4E/fishsense-services/commit/dbfc06429f32ca6cdeb7f3b00ef28a9fe1518e19))
* **processor:** stage 0.1 laser preprocessing and GPU laser prediction ([e136bc3](https://github.com/UCSD-E4E/fishsense-services/commit/e136bc3047910e5d69040d39e16d08373bc26112))
* **processor:** stage 5.1 renders the head/tail JPEG from the refs it is handed ([d696ca2](https://github.com/UCSD-E4E/fishsense-services/commit/d696ca248695c338f5d3e3d0f5e899f2a9715009))
* **processor:** stages 9 and 13, checkerboard calibration and lattice renders ([5ae9723](https://github.com/UCSD-E4E/fishsense-services/commit/5ae9723e09b3125cba26d4cdfc4a6000c44bec47))
* **processor:** the worker, image and compose service; stage 1 end to end ([3b5bf36](https://github.com/UCSD-E4E/fishsense-services/commit/3b5bf36c1782780243fb21f36e044834de75d335))
* **species:** stage 2 on the processor, and its contract ([e3e0bb2](https://github.com/UCSD-E4E/fishsense-services/commit/e3e0bb26fd5e4280ba86bb7ab331977fd53318df))
* **species:** the species stages on the orchestrator ([f7c84c8](https://github.com/UCSD-E4E/fishsense-services/commit/f7c84c82738f237d60c3bbc7f8532754c733377a))
* **species:** the species stages' database side, and how a refusal expires ([fd484a3](https://github.com/UCSD-E4E/fishsense-services/commit/fd484a330b3ee5bae01c649e4f0a091d76e61a64))
* the foundations for porting the rest of v1 ([b379a50](https://github.com/UCSD-E4E/fishsense-services/commit/b379a50e1c0ef30bceb5125f5596fc0c82a36e7e))
* the processing contract, and stage 1 (clustering) end to end ([ba586e2](https://github.com/UCSD-E4E/fishsense-services/commit/ba586e2a048431a31df08b14aeb70075f1944980))
* **web:** port the web portal onto the v2 API as apps/web ([8dfaab9](https://github.com/UCSD-E4E/fishsense-services/commit/8dfaab9a49229e7e6fa5ac25a57777eeeae47e21))


### Bug Fixes

* a processed JPEG stays where it is, and tasks show it there ([f365f41](https://github.com/UCSD-E4E/fishsense-services/commit/f365f41ba6b949bd09d938ca5a7b647f96e1caf6))
* **api:** a dive is measured with v1's effective calibration ([78ab4a8](https://github.com/UCSD-E4E/fishsense-services/commit/78ab4a84762c994fac6f37652ace8ff0ccf2ea3d))
* **api:** a duplicate device serial is a 409, not a 500 ([75eccff](https://github.com/UCSD-E4E/fishsense-services/commit/75eccff94fd05cfbf936e49a4982f5175c3dda28))
* **api:** a failed key refetch during rotation is an outage (503) ([e8215f7](https://github.com/UCSD-E4E/fishsense-services/commit/e8215f76fd57f49b64eb3255788dd2eb0f512c14))
* **api:** a laser dive with no camera is never a candidate ([8168def](https://github.com/UCSD-E4E/fishsense-services/commit/8168def96e2041f295786191b1030ad035609aaf))
* **api:** a slate or target write expires a calibration refusal ([f3a341e](https://github.com/UCSD-E4E/fishsense-services/commit/f3a341e0a015f7cc5c15d79943804d4486dd8a58))
* **api:** a v1 head/tail label with NULL superseded migrates as not live ([49749c3](https://github.com/UCSD-E4E/fishsense-services/commit/49749c3baa18cfeaba5e196e7be070e467c5e1ca))
* **api:** a v1 laser label with NULL superseded migrates as not live ([d19f0b8](https://github.com/UCSD-E4E/fishsense-services/commit/d19f0b8cfe672f46775fd1acde942930bebf5b3e))
* **api:** an unknown device kind is a 422, not a 500 ([2dd332b](https://github.com/UCSD-E4E/fishsense-services/commit/2dd332bce0473a283d3e97f1f54547bb64f6848f))
* **api:** an unusable JWKS response is an outage (503), not a 500 or 401 ([5a64f61](https://github.com/UCSD-E4E/fishsense-services/commit/5a64f6129a0638041014a50778ac2a87dcd36262))
* **api:** declare pipeline_status_01's indexes in the models ([c0f5df5](https://github.com/UCSD-E4E/fishsense-services/commit/c0f5df5df3b2ca8ef1aab4a6e8a5faff39cb2ec8))
* **api:** fit two constraints to real v1 data (migration 0014) ([fd7348e](https://github.com/UCSD-E4E/fishsense-services/commit/fd7348e338801b3fb94f8fe46a77d80725a6998c))
* **api:** head/tail cohorts leave out a dive stage 5.1 cannot render ([ef508ba](https://github.com/UCSD-E4E/fishsense-services/commit/ef508ba6cc69b9eadadd9307be8af7fae7ed138c))
* **api:** laser and species rectify only a pinhole camera, and offer only such dives ([c012341](https://github.com/UCSD-E4E/fishsense-services/commit/c012341663eb19bcf1c6525f9f5463ad1e7ad36b))
* **api:** measurement parity reads v1 the way v2 reads its copy ([ffae6a6](https://github.com/UCSD-E4E/fishsense-services/commit/ffae6a62d46e16a86b8d355970b69e8b2be776bc))
* **api:** migration 0005 handles device kinds written before the check ([bbe2f66](https://github.com/UCSD-E4E/fishsense-services/commit/bbe2f66b30ee161dcff7d28c139bed9d4d2f0dee))
* **api:** stop trusting signing keys after Authentik revokes them ([5002e2b](https://github.com/UCSD-E4E/fishsense-services/commit/5002e2b9513bc29863297395b1640fd8e45debbe))
* **api:** the calibration and stage-9 cohorts offer only dives their resolvers can resolve ([bd459f8](https://github.com/UCSD-E4E/fishsense-services/commit/bd459f8cd0f61f5feff93211a1e5035954a1976f))
* **api:** the laser sync never writes a superseded label ([2fe3e5d](https://github.com/UCSD-E4E/fishsense-services/commit/2fe3e5d12e6349ee0bf85164d6a398e5c55c6d53))
* **api:** the portal's gated filter reads the gate's effective verdict ([d907603](https://github.com/UCSD-E4E/fishsense-services/commit/d907603ad48e147ad6f3a206d5b14bb33faeb3a5))
* **api:** the tenancy audit checks policies exactly, not by mention ([08cbe6d](https://github.com/UCSD-E4E/fishsense-services/commit/08cbe6da9078b9625fe6cba30ece497f7b4a1ab6))
* **deploy:** a rehearsal without credentials never calls a production host ([839618b](https://github.com/UCSD-E4E/fishsense-services/commit/839618bc5208c950b636b48b5b0b68b1f626e965))
* **depth-measure:** a depth persists only at the dot it was computed at; tied dives drain by number ([7a865aa](https://github.com/UCSD-E4E/fishsense-services/commit/7a865aaed85a72ebf17ca885fcb3695ac3464f03))
* **depth-measure:** stale bindings retire only on high-priority dives; stage-14 work in linear time ([eaa9cae](https://github.com/UCSD-E4E/fishsense-services/commit/eaa9cae2489d3c40ef6f7bb610ede1b6bd200c54))
* **ops:** the cert sync rolls every processor Deployment onto a new leaf ([03cae12](https://github.com/UCSD-E4E/fishsense-services/commit/03cae12c7fbec659a2085d5645cccb42f9be93f0))
* **orchestrator:** a fresh wake is given time to reach its queue ([85f43ac](https://github.com/UCSD-E4E/fishsense-services/commit/85f43ac1874fd84c2101908169c3e3ec3d202f47))
* **orchestrator:** a migrated slate template is staged from the PDF v1 staged ([5e69c3d](https://github.com/UCSD-E4E/fishsense-services/commit/5e69c3d86b75c12ce84bf5477db850694a1baccc))
* **orchestrator:** a retried calibration record appends the attempt once ([284508d](https://github.com/UCSD-E4E/fishsense-services/commit/284508d7abaf88318b223d5aa4f4a7d3f2b8da02))
* **orchestrator:** a throttle after the import keeps IMPORT_ISSUED ([7d177f2](https://github.com/UCSD-E4E/fishsense-services/commit/7d177f23671f0458c38a27ae460d0d2f8b25d1f6))
* **orchestrator:** an empty NRP kubeconfig is the unseeded soft render, a no-op for cert sync ([f980ef7](https://github.com/UCSD-E4E/fishsense-services/commit/f980ef70ee0a71cd0dec6fdb9c7b0980bb8a4517))
* **orchestrator:** cleanup stops on a lost membership; a racing first write of the GPU state patches ([360cd22](https://github.com/UCSD-E4E/fishsense-services/commit/360cd22fd09129f741807f54ee9e100038728118))
* **orchestrator:** every Label Studio kind creates and populates on v1's one policy ([be9120d](https://github.com/UCSD-E4E/fishsense-services/commit/be9120da267d78935538e262ae75d88a29d91289))
* **orchestrator:** every per-dive kind declares its labeling config ([081f3f3](https://github.com/UCSD-E4E/fishsense-services/commit/081f3f33f34caafeceac01a7d8cce121e0f7601d))
* **orchestrator:** the head/tail predict run outlives its wake and child ([b9d151a](https://github.com/UCSD-E4E/fishsense-services/commit/b9d151afae91359a5d7cea61773879f5bab02e90))
* **orchestrator:** the laser auto-accept apply marks each task as it annotates it ([c2cbf97](https://github.com/UCSD-E4E/fishsense-services/commit/c2cbf972a6567ec96ca93113eb25fdf0f8bf7ddf))
* **orchestrator:** the laser populate heartbeats through its JPEG gate ([7248709](https://github.com/UCSD-E4E/fishsense-services/commit/724870992792400539cffdefb6c9aafe5bcd853f))
* **orchestrator:** two preflight messages the real-data dry run exposed ([697de22](https://github.com/UCSD-E4E/fishsense-services/commit/697de227adb43475598a165860b68328599150a8))
* **processor:** a head/tail abstention names the dot it was made from ([94a560a](https://github.com/UCSD-E4E/fishsense-services/commit/94a560a16f2c5d162ce2b99afd45326675550af1))
* **processor:** a SAM 3.1 weights failure is final, not a retry per image ([4d8bd76](https://github.com/UCSD-E4E/fishsense-services/commit/4d8bd76138b1f507080b0b8605b7573c9479134a))
* **processor:** blank weights settings fail at startup; pods cache weights on their volume ([efad358](https://github.com/UCSD-E4E/fishsense-services/commit/efad3584a854621bad01954e3f84cba4fcb5952a))
* **species:** a completed sentinel is done work in both cohorts and the resolver ([ea7aeba](https://github.com/UCSD-E4E/fishsense-services/commit/ea7aeba2bd156878b6e8b354a09c195562c5954d))
* **species:** populate skips a task a migrated duplicate already holds ([51afbd1](https://github.com/UCSD-E4E/fishsense-services/commit/51afbd174eb98f6a8ae83b2adf843ba1eaec68b3))
* **species:** stage 6.1 leaves a migrated duplicate out instead of refusing the dive ([e841326](https://github.com/UCSD-E4E/fishsense-services/commit/e841326a24dbd955730f67f6d1d3faa73bb19e89))
* **v1-migration:** skip and report v1's non-positive measurement lengths ([fad2462](https://github.com/UCSD-E4E/fishsense-services/commit/fad2462f6d25d66d0949cdf4fc65cbb4e61cc598))
* **web:** an Authentik or API outage leaves the landing page up ([48ec9dc](https://github.com/UCSD-E4E/fishsense-services/commit/48ec9dc2c1f5d452debe8bf7c312d8f335428c41))
* **web:** triage writes and streams only the tenant's own Label Studio tasks ([7e43e66](https://github.com/UCSD-E4E/fishsense-services/commit/7e43e66d6919ee75e38ebbf0772620339c31953f))


### Documentation

* adopt the calibration 'current' rule and same-tenant borrowing (9.13, 9.17) ([92f4067](https://github.com/UCSD-E4E/fishsense-services/commit/92f4067c56f5934c79fe40932a0f0cf65acb4153))
* **cutover:** v1's Superset keeps serving across the switch until the profile is on ([1e974db](https://github.com/UCSD-E4E/fishsense-services/commit/1e974db43b3646f64d7582c30b989202b873d6b1))
* **deploy:** the cutover runbook, and a rehearsal of the production compose ([32586a2](https://github.com/UCSD-E4E/fishsense-services/commit/32586a26d378347af9b55f8675ae2bf92729cb80))
* **orchestrator:** the labels package holds the write side too ([e1023dc](https://github.com/UCSD-E4E/fishsense-services/commit/e1023dc39a74631c29222a8dd0a8eb9db50a4796))
* **plan:** stand the NRP processor up and tear it down, rather than scale to zero ([5d61bc3](https://github.com/UCSD-E4E/fishsense-services/commit/5d61bc37729331ff171a0dc3d0410d4e15e57a6e))
* **plan:** the processing contract (§9.1) is built ([4b33485](https://github.com/UCSD-E4E/fishsense-services/commit/4b334856317829687694fcab0c3a3ba6fa72204d))
* **plan:** two-week cutover at full parity; shared bucket and Garage weights for now ([1431ffa](https://github.com/UCSD-E4E/fishsense-services/commit/1431ffaf1a4c3a226bf99b99bc983db9bb85a3cd))
* **port-plan:** what became of v1's operator tools and scripts ([ae69428](https://github.com/UCSD-E4E/fishsense-services/commit/ae69428ae89d2347245fbcd9f28a33143dd6ae46))
* record Python-over-TypeScript rationale, SQLAlchemy 2.0, and TDD ([66a6556](https://github.com/UCSD-E4E/fishsense-services/commit/66a655687ca28b060889e1223c672356e5fdd2f2))
* record tenant-in-path, deferred sharing, tenancy-first slice (9.10) ([0eb1638](https://github.com/UCSD-E4E/fishsense-services/commit/0eb16385f65e2b2ea1732009b2ed90e3d0986504))
* record the v2 schema conventions (PLAN 4.3) ([4bacee5](https://github.com/UCSD-E4E/fishsense-services/commit/4bacee50bf30d015c8338a0f2dc6f4c9e4e57a3d))
* refresh the v2 plan against fishsense-lite and the research repos ([5a352ae](https://github.com/UCSD-E4E/fishsense-services/commit/5a352aeff663f692130f23e46c4d21876b97d036))
* refresh v2 plan against fishsense-lite HEAD and the research repos ([02e1836](https://github.com/UCSD-E4E/fishsense-services/commit/02e1836c143e0d070d078e239d6e870b806bc703))
* switch delivery to a big-bang cutover on the existing slot ([54b93bd](https://github.com/UCSD-E4E/fishsense-services/commit/54b93bd380903ece2d49dccf6cd20db57cd1702c))
* the port plan and the per-slice maps of v1 ([a0de856](https://github.com/UCSD-E4E/fishsense-services/commit/a0de856511ba4c04f3c813125371035a8e7c3876))
* v2 architecture plan and diagrams ([c8a160f](https://github.com/UCSD-E4E/fishsense-services/commit/c8a160fd68f3f9455dc599dc6db8e9412114c996))
