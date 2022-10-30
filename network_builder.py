import docker
import requests
import os
import shutil
import toml, yaml
import logging
from cosmpy.aerial.wallet import LocalWallet
from bip_utils import Bip39MnemonicGenerator, Bip39WordsNum


def get_network_info(network):
    logging.debug(f"Fetching chain registry for network {network}")
    return requests.get(f"https://chains.cosmos.directory/{network}").json()


class NetworkBuilder:
    def __init__(self, network, name, config, docker_client):
        self.config = config
        self.docker_client = docker_client
        self.network = network
        self.network_name = name
        self.network_info = get_network_info(self.network)
        self.wallet_prefix = self.network_info["chain"]["bech32_prefix"]
        self.node_home = self.network_info["chain"]["node_home"].split("/")[1]
        self.binary_name = self.network_info["chain"]["daemon_name"]
        self.denom = self.network_info["chain"]["denom"]
        self.builder = None
        self.user = f"{os.getuid()}:{os.getgid()}"

    def generate_network(self):
        self._build_image()

        self._create_network_builder()
        self._generate_node_wallets()
        self._build_genesis()

        self.builder.remove(force=True)

        for node_name in self.config.keys():
            self._distribute_genesis(node_name)

        self._generate_peers()
        for node_name in self.config.keys():
            self._dump_config(node_name)

        self._generate_docker_compose()

        for node_name, info in self.config.items():
            if info["type"] == "validator":
                print(node_name, "mnemonic:", info["wallet"]["wallet"])

    def _build_image(self):
        for build_type in ["blue", "green"]:
            logging.info(f"Building image {self.network}-{build_type}")
            self.docker_client.images.build(
                path=f"{os.getcwd()}/dockerfiles/",
                dockerfile=f"{os.getcwd()}/dockerfiles/{self.network}.{build_type}",
                tag=f"bldr:{self.network}-{build_type}",
                buildargs={"distro_version": "debug"},
            )

    def _create_network_builder(self):
        logging.info("Launching builder container")
        self.builder = self.docker_client.containers.run(
            f"bldr:{self.network}-green",
            "infinity",  # making sure it just sleeps
            detach=True,
            entrypoint="sleep",
            remove=True,
            name=f"bldr-{self.network_name}-builder",
            user=self.user,
            volumes=[
                f"{os.getcwd()}/data/{self.network_name}/:/mnt/{self.network_name}/"
            ],
        )
        logging.info(f"Launched builder container {self.builder.id}")
        self.builder.exec_run(
            f"chown -R {os.getuid()}:{os.getgid()} /mnt/{self.network_name}"
        )

    def _generate_keys(self):
        mnemonic = Bip39MnemonicGenerator().FromWordsNumber(Bip39WordsNum.WORDS_NUM_12)
        wallet = LocalWallet.from_mnemonic(mnemonic, prefix=self.wallet_prefix)
        return {"mnemonic": mnemonic.ToStr(), "wallet": wallet}

    def _generate_genesis(self):
        logging.info("Generating initial genesis")
        output = self.builder.exec_run(
            f"{self.binary_name} init --home=/mnt/{self.network_name}/{self.node_home} --chain-id={self.network_name} builder",
            demux=False,
            user=self.user,
        )

        logging.debug(output[1].decode("utf-8"))
        sed = self.builder.exec_run(
            f'sed -i \'s/denom": "stake"/denom": "{self.denom}"/g\' /mnt/{self.network_name}/{self.node_home}/config/genesis.json',
            demux=False,
            user=self.user,
        )
        return output[1]

    def _create_home(self, node_name):
        # create home
        logging.info(f"Creating node home directory for {node_name}")
        self.builder.exec_run(
            f"{self.binary_name} init --home=/mnt/{self.network_name}/.{node_name} --chain-id={self.network_name} {node_name}",
            demux=False,
            user=self.user,
        )

        # copy genesis
        logging.info(f"Copying genesis to node {node_name}")
        self.builder.exec_run(
            f"cp /mnt/{self.network_name}/{self.node_home}/config/genesis.json /mnt/{self.network_name}/.{node_name}/config/genesis.json",
            demux=False,
            user=self.user,
        )

    def _get_node_id(self, node_name):
        node_id_output = self.builder.exec_run(
            f"{self.binary_name} tendermint show-node-id --home=/mnt/{self.network_name}/.{node_name}",
            demux=False,
            user=self.user,
        )

        return node_id_output[1].decode("utf-8").strip()

    def _import_keys(self, node_name, mnemonic):
        logging.info(f"Importing keys into node {node_name}")
        output = self.builder.exec_run(
            f"sh -c 'echo {mnemonic} | {self.binary_name} keys add --keyring-backend=test --recover --home=/mnt/{self.network_name}/.{node_name} {node_name}'",
            demux=False,
            user=self.user,
        )
        return output[1].decode("utf-8")

    def _build_gentx(self, node_name, wallet):
        logging.info(f"Adding genesis accounts into node {node_name}")
        add_acc_output = self.builder.exec_run(
            f"{self.binary_name} --home=/mnt/{self.network_name}/.{node_name} add-genesis-account {wallet} 200000000000{self.denom},2000000000uskip",
            demux=False,
            user=self.user,
        )

        logging.info(f"Generating genesis transaction into node {node_name}")
        gentx_output = self.builder.exec_run(
            f"{self.binary_name} --home=/mnt/{self.network_name}/.{node_name} --chain-id {self.network_name} --keyring-backend test gentx {node_name} 1000000000{self.denom}",
            demux=False,
            user=self.user,
        )

        self.builder.exec_run(
            f"mkdir -p /mnt/{self.network_name}/{self.node_home}/config/gentx",
            user=self.user,
        )

        logging.info(f"Copying {node_name} node's genesis transaction")
        cp_output = self.builder.exec_run(
            f"sh -c 'cp -r /mnt/{self.network_name}/.{node_name}/config/gentx/* /mnt/{self.network_name}/{self.node_home}/config/gentx'",
            demux=False,
            user=self.user,
        )

        return gentx_output[1].decode("utf-8")

    def _build_final_genesis(self, wallets):
        for wallet in wallets:
            if wallet == None:
                continue

            logging.info(
                f"Adding genesis account {wallet['wallet']} into final genesis"
            )
            add_acc_output = self.builder.exec_run(
                f"{self.binary_name} --home=/mnt/{self.network_name}/{self.node_home} add-genesis-account {wallet['wallet']} 2000000000{self.denom},2000000000uskip",
                demux=False,
                user=self.user,
            )

        logging.info(f"Collecting genesis transactions")
        build_gentx_output = self.builder.exec_run(
            f"{self.binary_name} --home=/mnt/{self.network_name}/{self.node_home} collect-gentxs",
            demux=False,
            user=self.user,
        )

        return build_gentx_output[1].decode("utf-8")

    def _generate_node_wallets(self):
        for node, info in self.config.items():
            if info["type"] == "validator":
                self.config[node]["wallet"] = self._generate_keys()
            else:
                self.config[node]["wallet"] = None

    def _build_genesis(self):
        genesis = self._generate_genesis()

        # for every node, generate a gentx transaction
        for node_name, info in self.config.items():
            self._create_home(node_name)
            if info["type"] == "validator":
                self._import_keys(node_name, mnemonic=info["wallet"]["mnemonic"])
                gentx = self._build_gentx(node_name, info["wallet"]["wallet"])

            node_id = self._get_node_id(node_name)
            self.config[node_name]["node_id"] = node_id

        wallets = [node[1]["wallet"] for node in self.config.items()]
        final_genesis = self._build_final_genesis(wallets)

    def _distribute_genesis(self, node_name):
        logging.info(f"Copying genesis files into node home {node_name}")
        shutil.copyfile(
            f"{os.getcwd()}/data/{self.network_name}/{self.node_home}/config/genesis.json",
            f"{os.getcwd()}/data/{self.network_name}/.{node_name}/config/genesis.json",
        )

    def _generate_peers(self):
        sentries = [
            f"{sentry['node_id']}@{name}:26656"
            for name, sentry in self.config.items()
            if sentry["type"] == "sentry"
        ]

        for node_name, info in self.config.items():
            persistent_peers = []
            if info["type"] == "validator":
                sentry = info["sentry"]
                sentry_info = self.config[sentry]
                persistent_peers = [f"{sentry_info['node_id']}@{sentry}:26656"]
            elif info["type"] == "sentry":
                persistent_peers = sentries
            else:
                print("a node is neither a sentry nor a validator")

            self.config[node_name]["persistent_peers"] = persistent_peers
            self.config[node_name]["seeds"] = persistent_peers

    def _dump_config(self, node_name):
        logging.info(f"Generating config.toml for node {node_name}")
        with open(
            f"{os.getcwd()}/data/{self.network_name}/.{node_name}/config/config.toml",
            "r",
        ) as config_file:
            config = config_file.read()

        persistent_peers = self.config[node_name]["persistent_peers"]

        parsed_config = toml.loads(config)
        parsed_config["p2p"]["persistent_peers"] = ",".join(persistent_peers)
        parsed_config["p2p"]["seeds"] = ""
        parsed_config["rpc"]["laddr"] = "tcp://0.0.0.0:26657"

        with open(
            f"{os.getcwd()}/data/{self.network_name}/.{node_name}/config/config.toml",
            "w",
        ) as config_file:
            toml.dump(parsed_config, config_file)

    def _generate_docker_compose(self):
        logging.info("Generating final docker-compose file")
        template = {
            "services": {},
            "version": "2",
            "networks": {
                "sentries": {
                    "driver": "bridge",
                }
            },
        }
        for i, (node_name, info) in enumerate(self.config.items()):
            template["networks"][node_name] = {"driver": "bridge"}
            service = {
                "container_name": node_name,
                "image": f"bldr:{self.network}-blue"
                if info["image"] == "blue"
                else f"bldr:{self.network}-green",
                "command": "start",
                "restart": "always",
                "ports": [f"{26657+i}:26657"],
                "networks": [node_name]
                if info["type"] == "validator"
                else ["sentries", info["validator"]],
                "volumes": [
                    f"{os.getcwd()}/data/{self.network_name}/.{node_name}:/root/{self.node_home}"
                ],
            }
            template["services"][node_name] = service

        with open(
            f"{os.getcwd()}/data/{self.network_name}/docker-compose.yml", "w"
        ) as compose_file:
            compose_file.write(yaml.dump(template, indent=4))
