#!/usr/bin/env python3
import argparse
import configparser
import logging
import logging.handlers
import os.path
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from io import StringIO
import requests
import json

# Global variables
config = None
email_log = None

# Discord webhook URL
discord_webhook_url = None

def tee_log(infile, out_lines, log_level):
    """
    Create a thread that saves all the output on infile to out_lines and
    logs every line with log_level
    """
    def tee_thread():
        for line in iter(infile.readline, ""):
            logging.log(log_level, line.rstrip())
            out_lines.append(line)
        infile.close()
    t = threading.Thread(target=tee_thread)
    t.daemon = True
    t.start()
    return t


# Function to send Discord notification
def send_discord_notification(success, log):
    payload = {
        "content": "SnapRAID job completed successfully." if success else "Error during SnapRAID job:",
        "embeds": [
            {
                "title": "Log",
                "description": log,
                "color": 65280 if success else 16711680
            }
        ]
    }

    try:
        response = requests.post(discord_webhook_url, data=json.dumps(payload), headers={"Content-Type": "application/json"})
        response.raise_for_status()
        logging.info("Discord notification sent successfully.")
    except requests.exceptions.HTTPError as errh:
        logging.error("HTTP Error: %s" % errh)
    except requests.exceptions.ConnectionError as errc:
        logging.error("Error Connecting: %s" % errc)
    except requests.exceptions.Timeout as errt:
        logging.error("Timeout Error: %s" % errt)
    except requests.exceptions.RequestException as err:
        logging.error("Something went wrong: %s" % err)

def snapraid_btrfs_command(command, *, snapraid_args={}, snapraid_btrfs_args={}, allow_statuscodes=[]):
    """
    Run snapraid-btrfs command
    Raises subprocess.CalledProcessError if errorlevel != 0
    """
    snapraid_btrfs_arguments = ["--quiet",
                                "--conf", config["snapraid"]["config"],
                                "--snapper-path", config["snapper"]["executable"],
                                "--snapraid-path", config["snapraid"]["executable"]]
    # if len(config["snapraid-btrfs"]["cleanup-algorithm"]) > 0:
    #     snapraid_btrfs_arguments.extend(["--cleanup", config["snapraid-btrfs"]["cleanup-algorithm"]])
    for (k, v) in snapraid_btrfs_args.items():
        snapraid_btrfs_arguments.extend(["--" + k, str(v)])
    if command == "cleanup":
        snapraid_arguments = []
    else:
        snapraid_arguments = ["--quiet"]
    for (k, v) in snapraid_args.items():
        snapraid_arguments.extend(["--" + k, str(v)])
    p = subprocess.Popen(
        [config["snapraid-btrfs"]["executable"]] + snapraid_btrfs_arguments + [command] + snapraid_arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        # Snapraid always outputs utf-8 on windows. On linux, utf-8
        # also seems a sensible assumption.
        encoding="utf-8",
        errors="replace"
    )
    out = []
    threads = [
        tee_log(p.stdout, out, logging.OUTPUT),
        tee_log(p.stderr, [], logging.OUTERR)]
    for t in threads:
        t.join()
    ret = p.wait()
    # sleep for a while to make pervent output mixup
    time.sleep(0.3)
    if ret == 0 or ret in allow_statuscodes:
        return out
    else:
        raise subprocess.CalledProcessError(ret, "snapraid-btrfs " + command)


def send_email(success):
    import smtplib
    from email.mime.text import MIMEText
    from email import charset

    if len(config["smtp"]["host"]) == 0:
        logging.error("Failed to send email because smtp host is not set")
        return

    # use quoted-printable instead of the default base64
    charset.add_charset("utf-8", charset.SHORTEST, charset.QP)
    if success:
        body = "SnapRAID job completed successfully:\n\n\n"
    else:
        body = "Error during SnapRAID job:\n\n\n"

    log = email_log.getvalue()
    maxsize = config['email'].get('maxsize', 500) * 1024
    if maxsize and len(log) > maxsize:
        cut_lines = log.count("\n", maxsize // 2, -maxsize // 2)
        log = (
            "NOTE: Log was too big for email and was shortened\n\n" +
            log[:maxsize // 2] +
            "[...]\n\n\n --- LOG WAS TOO BIG - {} LINES REMOVED --\n\n\n[...]".format(
                cut_lines) +
            log[-maxsize // 2:])
    body += log

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = config["email"]["subject"] + \
        (" SUCCESS" if success else " ERROR")
    msg["From"] = config["email"]["from"]
    msg["To"] = config["email"]["to"]
    smtp = {"host": config["smtp"]["host"]}
    if config["smtp"]["port"]:
        smtp["port"] = config["smtp"]["port"]
    if config["smtp"]["ssl"]:
        server = smtplib.SMTP_SSL(**smtp)
    else:
        server = smtplib.SMTP(**smtp)
        if config["smtp"]["tls"]:
            server.starttls()
    if config["smtp"]["user"]:
        server.login(config["smtp"]["user"], config["smtp"]["password"])
    server.sendmail(
        config["email"]["from"],
        [config["email"]["to"]],
        msg.as_string())
    server.quit()


def finish(is_success):
    if ("error", "success")[is_success] in config["email"]["sendon"]:
        try:
            send_email(is_success)
        except Exception:
            logging.exception("Failed to send email")

    if "discord" in config and config["discord"]["enabled"]:
        if ("error", "success")[is_success] in config["discord"]["sendon"]:
            try:
                send_discord_notification(is_success, email_log.getvalue())
            except Exception:
                logging.exception("Failed to send Discord notification")

    if is_success:
        logging.info("Run finished successfully")
    else:
        logging.error("Run failed")
    sys.exit(0 if is_success else 1)


def load_config(args):
    global config
    global discord_webhook_url

    parser = configparser.RawConfigParser()
    parser.read(args.conf)
    sections = ["snapraid-btrfs", "snapper", "snapraid", "logging", "email", "smtp", "scrub", "discord"]
    config = dict((x, defaultdict(lambda: "")) for x in sections)
    for section in parser.sections():
        for (k, v) in parser.items(section):
            config[section][k] = v.strip()

    int_options = [
        ("snapraid", "deletethreshold"), ("logging", "maxsize"),
        ("scrub", "older-than"), ("email", "maxsize"),
    ]
    for section, option in int_options:
        try:
            config[section][option] = int(config[section][option])
        except ValueError:
            config[section][option] = 0

    try:
        config["snapraid-btrfs"]["cleanup"]
    except KeyError:
        config["snapraid-btrfs"]["cleanup"] = ""

    config["smtp"]["ssl"] = (config["smtp"]["ssl"].lower() == "true")
    config["smtp"]["tls"] = (config["smtp"]["tls"].lower() == "true")
    config["scrub"]["enabled"] = (config["scrub"]["enabled"].lower() == "true")
    config["email"]["short"] = (config["email"]["short"].lower() == "true")
    config["snapraid"]["touch"] = (config["snapraid"]["touch"].lower() == "true")
    config["snapraid-btrfs"]["pool"] = (config["snapraid-btrfs"]["pool"].lower() == "true")
    config["snapraid-btrfs"]["cleanup"] = (config["snapraid-btrfs"]["cleanup"].lower() == "true")

    if "discord" in config and config["discord"]["enabled"]:
        discord_webhook_url = config["discord"]["webhook_url"]

    # Migration
    if config["scrub"]["percentage"]:
        config["scrub"]["plan"] = config["scrub"]["percentage"]

    if args.scrub is not None:
        config["scrub"]["enabled"] = args.scrub

    if args.deletethreshold is not None:
        config["snapraid"]["deletethreshold"] = args.deletethreshold

    if args.ignore_deletethreshold:
        config["snapraid"]["deletethreshold"] = -1

    if args.pool is not None:
        config["snapraid-btrfs"]["pool"] = args.pool

    if args.cleanup is not None:
        config["snapraid-btrfs"]["cleanup"] = args.cleanup


def setup_logger():
    log_format = logging.Formatter(
        "%(asctime)s [%(levelname)-6.6s] %(message)s")
    root_logger = logging.getLogger()
    logging.OUTPUT = 15
    logging.addLevelName(logging.OUTPUT, "OUTPUT")
    logging.OUTERR = 25
    logging.addLevelName(logging.OUTERR, "OUTERR")
    root_logger.setLevel(logging.OUTPUT)
    console_logger = logging.StreamHandler(sys.stdout)
    console_logger.setFormatter(log_format)
    root_logger.addHandler(console_logger)

    if config["logging"]["file"]:
        max_log_size = max(config["logging"]["maxsize"], 0) * 1024
        file_logger = logging.handlers.RotatingFileHandler(
            config["logging"]["file"],
            maxBytes=max_log_size,
            backupCount=9)
        file_logger.setFormatter(log_format)
        root_logger.addHandler(file_logger)

    if config["email"]["sendon"]:
        global email_log
        email_log = StringIO()
        email_logger = logging.StreamHandler(email_log)
        email_logger.setFormatter(log_format)
        if config["email"]["short"]:
            # Don't send programm stdout in email
            email_logger.setLevel(logging.INFO)
        root_logger.addHandler(email_logger)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--conf",
                        default="snapraid-btrfs-runner.conf",
                        metavar="CONFIG",
                        help="Configuration file (default: %(default)s)")
    parser.add_argument("--no-pool", action='store_false',
                        dest='pool', default=None,
                        help="Do not update/create snapraid-btrfs pool (overrides config")
    parser.add_argument("--no-cleanup", action='store_false',
                        dest='cleanup', default=None,
                        help="Do not clean up snapraid-btrfs snapshots (overrides config")
    parser.add_argument("--no-scrub", action='store_false',
                        dest='scrub', default=None,
                        help="Do not scrub (overrides config)")
    parser.add_argument("--ignore-deletethreshold", action='store_true',
                        help="Sync even if configured delete threshold is exceeded (replaces --deletethreshold option)")
    parser.add_argument("-d", "--deletethreshold", type=int,
                        default=None, metavar='N',
                        help="Number of deletes to allow (overrides config) (deprecated, use --ignore-deletethreshold)")
    args = parser.parse_args()

    if not os.path.exists(args.conf):
        print("snapraid-btrfs-runner configuration file not found")
        parser.print_help()
        sys.exit(2)

    try:
        load_config(args)
    except Exception:
        print("unexpected exception while loading config")
        print(traceback.format_exc())
        sys.exit(2)

    try:
        setup_logger()
    except Exception:
        print("unexpected exception while setting up logging")
        print(traceback.format_exc())
        sys.exit(2)

    try:
        run()
    except Exception:
        logging.exception("Run failed due to unexpected exception:")
        finish(False)


def run():
    logging.info("=" * 60)
    logging.info("Run started")
    logging.info("=" * 60)

    if shutil.which(config["snapraid"]["executable"]) is None:
        logging.error("The configured snapraid executable \"{}\" does not "
                        "exist or is not a file".format(
                            config["snapraid"]["executable"]))
        finish(False)
    if not os.path.isfile(config["snapraid"]["config"]):
        logging.error("Snapraid config does not exist at " +
                        config["snapraid"]["config"])
        finish(False)
    if shutil.which(config["snapraid-btrfs"]["executable"]) is None:
        logging.error("Snapraid-btrfs executable does not exist at " +
                        config["snapraid-btrfs"]["executable"])
        finish(False)
    if shutil.which(config["snapper"]["executable"]) is None:
        logging.error("Snapper executable does not exist at " +
                        config["snapper"]["executable"])
        finish(False)

    snapraid_btrfs_args_extend = {}
    # snapraid_args_extend = {}

    if len(config["snapraid-btrfs"]["snapper-configs"]) > 0:
        snapraid_btrfs_args_extend["snapper-configs"] = config["snapraid-btrfs"]["snapper-configs"]
    if len(config["snapraid-btrfs"]["snapper-configs-file"]) > 0:
        snapraid_btrfs_args_extend["snapper-configs-file"] = config["snapraid-btrfs"]["snapper-configs-file"]

    if config["snapraid"]["touch"]:
        logging.info("Running touch...")
        snapraid_btrfs_command("touch", snapraid_btrfs_args = snapraid_btrfs_args_extend)
        logging.info("*" * 60)

    logging.info("Running diff...")
    diff_out = snapraid_btrfs_command("diff", snapraid_btrfs_args = snapraid_btrfs_args_extend, allow_statuscodes=[2])
    logging.info("*" * 60)

    diff_results = Counter(line.split(" ")[0] for line in diff_out)
    diff_results = dict((x, diff_results[x]) for x in
                        ["add", "remove", "move", "update"])
    logging.info(("Diff results: {add} added,  {remove} removed,  " +
                    "{move} moved,  {update} modified").format(**diff_results))

    if (config["snapraid"]["deletethreshold"] >= 0 and
            diff_results["remove"] > config["snapraid"]["deletethreshold"]):
        logging.error(
            "Deleted files exceed delete threshold of {}, aborting".format(
                config["snapraid"]["deletethreshold"]))
        logging.error("Run again with --ignore-deletethreshold to sync anyways")
        finish(False)

    if (diff_results["remove"] + diff_results["add"] + diff_results["move"] +
            diff_results["update"] == 0):
        logging.info("No changes detected, no sync required")
    else:
        logging.info("Running sync...")
        try:
            snapraid_btrfs_command("sync", snapraid_btrfs_args = snapraid_btrfs_args_extend)
        except subprocess.CalledProcessError as e:
            logging.error(e)
            finish(False)
        logging.info("*" * 60)

    # pool
    if config["snapraid-btrfs"]["pool"]:
        logging.info("Running pool...")
        if len(config["snapraid-btrfs"]["pool-dir"]) > 0:
            snapraid_btrfs_args_extend["pool-dir"] = config["snapraid-btrfs"]["pool-dir"]
        try:
            snapraid_btrfs_command("pool", snapraid_btrfs_args = snapraid_btrfs_args_extend)
        except subprocess.CalledProcessError as e:
            logging.error(e)
            finish(False)
        logging.info("*" * 60)

    # cleanup
    if config["snapraid-btrfs"]["cleanup"]:
        logging.info("Running cleanup...")
        try:
            snapraid_btrfs_command("cleanup", snapraid_btrfs_args = snapraid_btrfs_args_extend)
        except subprocess.CalledProcessError as e:
            logging.error(e)
            finish(False)
        logging.info("*" * 60)

    if config["scrub"]["enabled"]:
        logging.info("Running scrub...")
        # if using new, bad, or full, ignore older-than config option
        try:
            int(config["scrub"]["plan"])
        except ValueError:
            snapraid_args_extend = {
                "plan": config["scrub"]["plan"],
            }
            if config["scrub"]["older-than"] > 0:
                logging.warning(
                    "Ignoring 'older-than' config item with scrub plan '{}'".format(
                        config["scrub"]["plan"]))
        else:
            snapraid_args_extend = {
                "plan": config["scrub"]["plan"],
                "older-than": config["scrub"]["older-than"],
            }
        try:
            snapraid_btrfs_command("scrub", snapraid_args = snapraid_args_extend, snapraid_btrfs_args = snapraid_btrfs_args_extend)
        except subprocess.CalledProcessError as e:
            logging.error(e)
            finish(False)
        logging.info("*" * 60)

    logging.info("All done")
    finish(True)

main()
