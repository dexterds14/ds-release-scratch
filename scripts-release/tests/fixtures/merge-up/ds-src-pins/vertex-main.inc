# Source pins for every DS-built source that is fetched from a moving branch (ENG-1739).
#
# Every build pins every source it fetches. No committed recipe, conf or template may float a
# source with the AUTOREV variable, or with the literal it expands to: scripts/check-src-pins.sh
# fails CI if one does.
# An app change reaches this branch only through a reviewed bump of a pin below, together with any
# recipe change it needs. See .ai/source-pinning.md (AI) and docs/source-pinning.md (people).
#
# Rules for a pin:
# - a full 40-hex commit, never a branch name, tag name or short sha;
# - on the branch its recipe fetches (the *_BRANCH next to it). Most of these recipes
#   have no nobranch=1, so bitbake refuses a commit that is not on that branch;
# - weak (?=). bitbake.conf reads local.conf before this distro conf, so a developer or CI can
#   still override any pin from local.conf. CI uses that to build the ds commit under test.
#
# To float on your own machine, set the variable in YOUR build/conf/local.conf (never in a
# committed file), e.g. set DSREV to the AUTOREV variable. The build warns about it and CI fails
# on it (DS_SRC_PINS_STRICT, classes/ds-src-pins-guard.bbclass).

# ds.git (DS_BRANCH): every ds.git recipe. ds-ais-publisher, ds-announcer,
# ds-controller, ds-cot-xlate, ds-gps-publisher, ds-interpreter, ds-mqtt-xlate, ds-planner,
# ds-processing, ds-sdr, ds-services, ds-utils and mahalanobis, plus ds-config-variant, which
# takes containers/config.ini, the plans in containers/sdr/ and the variant overlay in
# containers/config/<variant>/ (ENG-1951, ENG-2118). None of them has nobranch=1, so this must be
# a commit on the ds branch DS_BRANCH names. It was the bare BRANCH, which also reached upstream
# recipes that read BRANCH ?= (pm-qa, ufs-utils).
# The read-only root stack (ENG-1597, ENG-1872, ENG-1768) needs ds #562 (the config overlay and
# [paths]) and #566 (the B3 residue on main). 1e4bc1a0 is the v1.0.0 tag: both, plus the schemas
# v0.6.0 pin (#569).
# vertex/1.0.x follows ds bulbasaur, the ds 1.0.x line, not main: main has moved on past v1.0.0.
# df25cd31 is v1.0.0 plus #576 (drop the empty [spectral_cfbw] sections) and #581 (the stream
# recorder reads its disk-full limit from fs_full_thresh), the 1.0.1 fix. 041abf26 adds #582
# (ENG-2118, the backport of #580: the plans and config values ds-config-variant ships).
# 91d40e76 is ds main at #591 (versions/ds 2.0.0). Past 041abf26 it brings #584 (the heartbeat
# carries the schemas version the controller was built against, from schemas/schemas_version.h,
# so the schemas pin below must be v0.6.2 or later), #587 (the SDK build), #588 (one version
# module, containers/cmake/ds-version.cmake, reading versions/ds) and #589 (the release-line
# tooling). The -DYOCTO_WORKDIR side file ds-versioning.bbclass writes here is no longer read:
# the binaries report the version ds-version.cmake derives from the tree.
# 9764de8d is ds 1.x.x, the minor line opened from v1.0.0 at 91d40e76 (identical to main there),
# plus #593: the heartbeat carries the chip ECID (HeartBeat.ecid, schemas v0.6.3, ENG-1921).
# 43684eda is the v1.0.1 tag on the ds 1.0.x line, which was opened from 91d40e76. Its one commit
# past 91d40e76 sets versions/ds to 1.0.1, so the binaries report 1.0.1. vertex/1.x.x takes the
# release on purpose, and leaves the 1.x.x-only #593 (heartbeat ECID) and #595 (log levels,
# [logging] level, the journal flood fixes) behind. The schemas pin stays at v0.6.3: a superset
# of what v1.0.1 needs (v0.6.2), so HeartBeat.ecid is just left empty.
# a9f1a3bc is ds 1.x.x at the merge of #598: ds_algos is the containers/third-party/ds_algos
# submodule, compiled into processing (no ds-algos recipe, no DS_ALGOS_REV; ds-processing fetches
# with gitsm). It contains v1.0.1 (43684eda) and brings back the 1.x.x work the 1.0.1 pin left
# behind: #593 (HeartBeat.ecid, which the v0.6.3 schemas pin below already carries) and #595 (log
# levels, [logging] level, the journal flood fixes). versions/ds there is 1.1.0, so the binaries
# report 1.1.0-dev.<N>.g<sha7>, not 1.0.1.
DS_BRANCH ?= "1.x.x"
# bd3e3237 is ds 1.x.x at the merge of #601: schemas is the containers/third-party/schemas
# submodule, compiled by every service (no ds-schemas recipe; the recipes fetch with gitsm), at
# f5a5e0bb (v0.6.5 plus schemas#95, dwell outputs). On top of c8a577cf (#597, the eventlog service
# and trace IDs) it brings #594 (radar gate threshold), #592 (descriptive name from the hostname
# on first boot), #585 (anomaly models), #573 (dwell outputs) and #602 (sdr.plan.saved/failed).
# c5664d16 is ds 1.x.x at the merge of ds #607. On top of bd3e3237 it brings ds #603 (ds-eventlog
# in ds's release config and a CI check, nothing built) and ds #607 (the rolling recording buffer
# in processing's SigmfRecorder; the schemas submodule moves from f5a5e0bb to bc89a4bd = v0.6.6,
# the commit sensor-portal builds at SENSOR_PORTAL_REV below).
# 11d7208f is ds 1.x.x at the merge of ds #620. On top of c5664d16 it brings ds #590 (GPS
# connectors: gps-publisher becomes a oneshot that starts one gps-connector@<source> unit per GPS
# source; the sandbox, A/B gate, ds-dev guard and sideload support for the pair, and curl in
# ds-gps-publisher's DEPENDS, are ENG-2342 in this layer), ds #612 (scripts/sdk-push.sh), ds #613
# (AIS vessel events), the 1.0.x merge-ups that carry v1.0.2 (30b46cf0: ds #615, the sdr gain
# limit follows the active radio plan, ENG-2346) and ds #620 (eventlog watches every
# gps-connector@ worker for crashes; its config.ini line is what makes the image's eventlog do it).
# It contains every ds 1.0.x commit vertex/1.3.x pins, up to v1.0.2.
# 11d7208f is also the v1.1.0 tag: the 1.x.x promote released it unchanged (ds release v1.1.0;
# the "pending 1.2.0" bump after it is not pinned).
DSREV ?= "11d7208fad6d767b3a89ef87314c94874f53cc5b"
# The release at DSREV, X.Y.Z, or empty when DSREV is not a release tag. ds-app.bbclass passes it
# to the ds build as DS_BUILD_VERSION, so the services report it. Without it they derive their
# version from the fetched tree, and a tag created after a mirror or sstate already holds the
# commit is never seen (the binaries kept reporting 1.1.0-dev.1110.g11d7208). The build fails
# when versions/ds at DSREV says otherwise, so a later pin bump cannot carry a stale value.
DSREV_RELEASE ?= "1.1.0"

# sensor-portal.git (SENSOR_PORTAL_BRANCH): ds-sensor-portal. The recipe fetches with nobranch=1,
# so bitbake does not check the branch, but the binary stamps it as GitBranch: set it to the
# portal line the pin sits on.
# 564ef6af is main at the merge of sensor-portal #275 (either status LED set from the LEDs and
# Services pages, the choice read as the watcher reads it from /config), on the v3.0.0 tag
# c53d022c: #253 (paths from [paths], ENG-1597), the read-only root work on top of it (#255
# B1/B2, #256 B6, ENG-1947) and schemas v0.6.0 (#262). It also carries #272, a sandbox/Dockerfile
# fix the recipe does not build. #275 needs the `set-unit <1|2> ...` forms of
# ds-status-led-watch and the /run/ds-status-led/conf symlink to
# /config/identity/status-led/conf from this layer.
# a3119fea: 3.x.x after merge-up #287 brought in #281, sensor-portal-keys.sh making the TLS
# pair again when its SAN does not name the host (ENG-2162), on top of 564ef6af (#275).
# 4a38b26c is the 3.0.x line at #285: v3.0.0 plus the release-line tooling (#286, which also moves
# swag's output from docs/ to swagger/), the TLS pair fix (#281), #277 and #285. It does not have
# #275: its LED page uses the one-argument `set-unit <unit>` form, which ds-status-led-watch
# still takes as indicator 2, and reads /run/ds-status-led/conf.
# 38a8b445 is the v3.0.1 tag on 3.0.x. Past 4a38b26c it brings #288 (shipped engines in the model
# list, never null, ENG-2098), the version dialog (image version, A/B slots from
# `ds-ota status --json`, schemas versions), #265 and #296 (recording fixes). The version dialog
# imports go-schemas/version, which the recipe writes from the schemas submodule.
# 74685922 is sensor-portal 3.x.x at the merge of #292, the minor line past v3.0.1: the events
# page, live event streams and trace IDs on every command, reading events.db where ds eventlog
# writes it. This moves the portal from 3.0.x to 3.x.x; versions/portal there is 3.1.0.
SENSOR_PORTAL_BRANCH ?= "3.x.x"
# 5b94dda0 is 3.x.x at sensor-portal #322 (delete all chunks of a recording), after sensor-portal
# #321 (events trace filter width) and sensor-portal #320 (developer-mode banner,
# scripts/sdk-push.sh), plus the 3.x.x work since 39551551.
# 23d765c9 is 3.x.x at sensor-portal #329, a merge-up of 3.0.x. Past 5b94dda0 it brings only 3.0.x
# work: sensor-portal #318 (NetBird server move, ENG-2332, with three new netbird_helper.sh sudo
# rules from the portal tree), #324 to #327 and the v3.0.2 tag (90d64947), plus CI and release
# script commits. It contains every sensor-portal 3.0.x commit vertex/1.3.x pins, up to v3.0.2.
# 23d765c9 is also the v3.1.0 tag: the 3.x.x promote released it unchanged (sensor-portal
# release v3.1.0; the "pending 3.2.0" bump after it is not pinned).
SENSOR_PORTAL_REV ?= "23d765c9b6e6888bbf6f2f899f0a12035be09d03"
# The release at SENSOR_PORTAL_REV, X.Y.Z, or empty. The portal recipe stamps it as
# handlers.GitTag (v3.1.0) instead of running git describe, which saw v3.0.2 for the same commit
# for the same reason as DSREV_RELEASE. The build fails when versions/portal disagrees.
SENSOR_PORTAL_RELEASE ?= "3.1.0"

# schemas.git has no pin and no recipe here: every ds service compiles ds's
# containers/third-party/schemas submodule (ds cmake/ds-schemas.cmake), so it moves with DSREV.
# (DS_SCHEMAS_REV and the ds-schemas recipe until then, last at a6485b59 = v0.6.4; sensor-portal
# has its own submodule.)

# model-licensing.git (LICENCE_BRANCH, default main): ds-licensing.
DS_LICENSING_REV ?= "0320cd2263a1c6297fc98b1ec7377a0b3fa9dc78"

# mrsi_service.git (main): mrsilib.
MRSILIB_REV ?= "2cff8b9328d65d0def61d875ade3890be5f2d9d4"

# sps-client.git (main): sps-client. Its crates are not locked (no Cargo.lock in the repo), so
# this pins the client's own code only.
SPS_CLIENT_REV ?= "70e677a477509f942305d907d5f5a14e73b384b3"

# ds_algos.git has no pin and no recipe here: ds-processing compiles ds's
# containers/third-party/ds_algos submodule into its binary (ds #598), so it moves with DSREV.

# bladeRF.git, the DS fork (BLADERF_BRANCH, default master): ds-bladerf. 4b68962a is the
# libbladeRF_v2.10.0 release tag commit. Keep the recipe's PV and the FPGA/FX3 image bundles in
# step when this moves (.ai/bladerf-stack.md).
BLADERF_REV ?= "4b68962a1bf40ecb9cc7bb974f05b64b8d4c94fd"

# deepsig/libsigmf (main): sigmf.
SIGMF_REV ?= "b87f8ff5163fa0dab7cc932cff22cc2d7709384e"

# ml-schema-contracts.git (CAPTURES_SCHEMA_BRANCH, default release/v1.14.0): captures-arrow-schema.
CAPTURES_SCHEMA_REV ?= "2b58976b4e712caebd2f7fdcb7ace5d36c76c4ac"

# geolocator.git (GEOLOCATOR_BRANCH, default master): geolocator, in the aggregator image. Bump it
# together with the recipe's files/config.json, which is written against this commit
# (.ai/aggregator-image.md).
GEOLOCATOR_SRCREV ?= "f5379d1cafcc941a831e38eb9c1968c5e92ce829"

# ds.git, branch experiment/yocto-target (set in the recipe's SRC_URI, not DS_BRANCH): ds-tui. Its
# own variable because the commit is not on ds main, so it cannot share DSREV. ds-tui is in no
# image today.
DS_TUI_REV ?= "9b06928ac8a3e9722e0e33842563f0adfd824b60"

# Every pin above. ds-src-pins-guard checks each one, and image-buildinfo writes each one into
# /etc/buildinfo, so an image says exactly which app code it contains. Add new pins here too.
DS_SRC_PIN_VARS = "DSREV SENSOR_PORTAL_REV DS_LICENSING_REV MRSILIB_REV \
                   SPS_CLIENT_REV BLADERF_REV SIGMF_REV CAPTURES_SCHEMA_REV \
                   DS_TUI_REV GEOLOCATOR_SRCREV"

INHERIT += "ds-src-pins-guard image-buildinfo"
# The plain IMAGE_BUILDINFO_VARS assignment lives in conf/distro/include/ds-build-version.inc
# (ENG-1930), which dsensos.conf requires first; this file is required last. The :append holds
# either way: bitbake applies :append when the variable is expanded, after every plain
# assignment has been parsed, so it would survive a later `=` too. What it needs is one plain
# assignment somewhere: with only an :append, image-buildinfo's own ?= would see the variable
# as already set and drop DISTRO and DISTRO_VERSION. That assignment is the `=` over there, so
# do not add a ?= here.
IMAGE_BUILDINFO_VARS:append = " ${DS_SRC_PIN_VARS}"
# No vardeps on the pins here: that would expand them while image recipes are parsed, and a
# developer's local float then fails every image with "AUTOREV/SRCPV set too late". A pin bump
# changes the packages, which reruns do_rootfs (and buildinfo with it) anyway.
