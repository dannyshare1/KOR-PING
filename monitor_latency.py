name: OCI Latency Optimizer (Hangzhou NAS)

on:
  workflow_dispatch:
    inputs:
      instance_ocid:
        description: "Target OCI instance OCID"
        required: true

jobs:
  run:
    runs-on: [self-hosted, Linux, X64]
    timeout-minutes: 30
    container:
      image: python:3.11
    steps:
      - uses: actions/checkout@v4

      - name: Install tools & SDK
        run: |
          apt-get update
          apt-get install -y iputils-ping iproute2
          pip install --no-cache-dir oci

      - name: Optimize latency
        env:
          OCI_CLI_USER:        ${{ secrets.OCI_CLI_USER }}
          OCI_CLI_TENANCY:     ${{ secrets.OCI_CLI_TENANCY }}
          OCI_CLI_REGION:      ${{ secrets.OCI_CLI_REGION }}
          OCI_CLI_FINGERPRINT: ${{ secrets.OCI_CLI_FINGERPRINT }}
          OCI_CLI_KEY_CONTENT: ${{ secrets.OCI_CLI_KEY_CONTENT }}
          OCI_CLI_PASSPHRASE:  ${{ secrets.OCI_CLI_PASSPHRASE }}
          OCI_INSTANCE_ID:     ${{ github.event.inputs.instance_ocid }}
          LATENCY_THRESHOLD_MS: ${{ secrets.LATENCY_THRESHOLD_MS }}
          PING_COUNT:           ${{ secrets.PING_COUNT }}
          MAX_SWITCHES:         ${{ secrets.MAX_SWITCHES }}
        run: python monitor_latency.py
