## Enterprise Log Analyzer - Backend

### Windows log dataset (27 GB)
Download the large Windows logs archive from Zenodo:

- Direct link: `https://zenodo.org/records/8196385/files/Windows.tar.gz?download=1`

Example commands to fetch and extract locally into the `data/` folder:

```bash
# Create data directory if missing
mkdir -p data

# Using curl
curl -L -o data/Windows.tar.gz "https://zenodo.org/records/8196385/files/Windows.tar.gz?download=1"

# Or using wget
wget -O data/Windows.tar.gz "https://zenodo.org/records/8196385/files/Windows.tar.gz?download=1"

# Verify and extract (may take a while; archive is ~27 GB)
ls -lh data/Windows.tar.gz
tar -xzvf data/Windows.tar.gz -C data
```

If you prefer streaming extraction to save disk space, you can do:

```bash
curl -L "https://zenodo.org/records/8196385/files/Windows.tar.gz?download=1" | tar -xz -C data
```

Note: Ensure you have enough free disk space (>= 60 GB recommended) before download and extraction.
