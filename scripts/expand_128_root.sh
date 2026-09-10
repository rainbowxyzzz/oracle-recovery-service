#!/bin/bash
set -euo pipefail

backup="/root/partition-table-before-expand-$(date +%Y%m%d-%H%M%S).sfdisk"
sfdisk -d /dev/sda > "$backup"
parted -s /dev/sda resizepart 3 100%
partprobe /dev/sda || true
udevadm settle || true
xfs_growfs /
echo "partition_backup=$backup"
lsblk
df -h /
