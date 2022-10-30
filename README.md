# bldr

## What it does
bldr automatically generates configuration and spins up a local testnet on your dev machine. As long as you have the correct Dockerfiles in `dockerfiles/`, all additional information is fetched from the Cosmos chain registry.

## How to use

```
python3 -m venv venv
source ./venv/bin/activate
pip install -r requirements.txt
cp config.yaml.example config.yaml
python main.py
cd data/bldr-osmosis-1
docker compose up
docker ps
```
