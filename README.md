# Snapraid-BTRFS Runner Script

This script is based on the [Chronial/snapraid-runner](https://github.com/Chronial/snapraid-runner)
project and runs [snapraid-btrfs](https://github.com/automorphism88/snapraid-btrfs), sending
its output to the console, a log file and via email. All this is configurable.

It can be run manually, but its main purpose is to be run via cronjob or systemd unit.

Given the use of the BTRFS filesystem, this script is only supported on Linux. It requires at least python3.7.

## How to use
* If you don’t already have it, download and install
  [the latest python version](https://www.python.org/downloads/).
* Clone this repository via git.
* Copy/rename the `snapraid-btrfs-runner.conf.example` to `snapraid-btrfs-runner.conf` and
  edit its contents. You need to at least configure the following configuration items:
  * `snapraid-btrfs.executable`
  * `snapper.executable`
  * `snapraid.executable`
  * `snapraid.config`
* Run the script via `python3 snapraid-btrfs-runner.py`.

## Features

Includes all the snapraid-runner features:
* Runs `diff` before `sync` to see how many files were deleted and aborts if
  that number exceeds a set threshold.
* Can create a size-limited rotated logfile.
* Can send notification emails after each run or only for failures.
* Can run `scrub` after `sync`

Includes basic snapraid-btrfs feature of taking BTRFS snapshots in conjunction with snapraid operations (i.e. 
sync, scrub, fix, etc.). This script omits access to many underlying snapraid-btrfs options, given its intended 
use as an unattended automatic snapraid tool.

## TODO
* Fix cleanup task. Currently this script doesn't actually doing any snapshot cleanup even if configured to do so.
* Provide config options for `--snapper-configs` and `--snapper-configs-file` underlying options in `snapraid-btrfs`.

## Changelog
### Unreleased master
* Initial commit based on snapraid-runner commit 68a03ce
