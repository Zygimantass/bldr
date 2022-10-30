import docker
import os
import argparse
import shutil
import yaml
import sys
import logging
from network_builder import NetworkBuilder


def main():
    parser = argparse.ArgumentParser(
        prog="bldr",
    )
    parser.add_argument(
        "-l",
        "--log",
        dest="logLevel",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level",
    )

    parser.add_argument(
        "-f",
        "--force",
        help="Overwrite any existing/in progress network build",
        action="store_true",
    )
    parser.add_argument(
        "-c",
        "--config-file",
        help="Path to config file",
        type=argparse.FileType("r"),
        default="config.yaml",
    )
    args = parser.parse_args()
    if args.logLevel:
        logLevel = getAttr(logging, args.logLevel)
    else:
        logLevel = "INFO"
    logging.basicConfig(level=logLevel, format="%(asctime)s %(levelname)s %(message)s")

    client = docker.from_env()
    config = yaml.safe_load(args.config_file)

    if args.force:
        try:
            container = client.containers.get(
                f"bldr-{config['network_name']}-builder"
            )
            container.remove(force=True)
        except docker.errors.NotFound:
            pass

        try:
            shutil.rmtree(f"{os.getcwd()}/data/{config['network_name']}")
        except FileNotFoundError:
            pass

    os.makedirs(f"{os.getcwd()}/data/{config['network_name']}")

    nb = NetworkBuilder(
        config["network"], config["network_name"], config["nodes"], client
    )
    nb.generate_network()


main()
