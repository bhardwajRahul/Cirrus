# Mirage - Collector

## Overview<hr>

Mirage Collector gathers forensic artifacts across a Google Cloud environment.

## Capabilities<hr>

Mirage Collector is a wrapper script that encloses two separate modules:

1. **Google Workspace/Cloud Identity Collector**
2. **Google Cloud Platform (GCP) Collector**

## Usage<hr>

### Prerequisites

- Mirage Collector supports and was tested with **Python3**

### Installation & Quick Start

1. Clone repository
2. (Optional) Set up virtual environment
3. Install dependencies
4. Obtain service account key file (via [Mirage Assistant](../Assistant/README.md) or other method)
5. Specify Google Workspace (`gw`) or Google Cloud Platform (`gcp`), associated flags, and execute script

```
Usage examples:
        mirage.py gw --key-file /path/to/creds.json --super-admin admin@example.com --override-cache all
        mirage.py gcp --key-file /path/to/creds.json logs --project-id test-project-1,test-project-2 --logs all_logs --start-time 2022-01-01T00:00:00Z --end-time 2022-01-08T00:00:00Z
```

### Google Workspace Collector

For detailed information regarding Google Workspace or Cloud Identity evidence collection,
visit [reference documentation](./Collectors/collectors/gw/README.md).

### Google Cloud Platform Collector

For detailed information regarding GCP evidence collection,
visit [reference documentation](./Collectors/collectors/gcp/README.md).

### Compatability

The collectors were developed and tested using Python v3.9.
The collectors can be executed from both standalone machines or directly from Google Cloud Shell as the
Mirage Assistant.  