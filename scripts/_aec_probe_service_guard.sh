#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Shared remote service guard for the hardware AEC probes. This file is
# concatenated ahead of each probe's remote body; it is not run on its own.

shairport_was_active=0
nqptp_was_active=0
voice_was_active=0
bridge_was_active=0

unit_active() {
  sudo systemctl is-active --quiet "$1"
}

stop_if_active() {
  local unit="$1"
  local state_var="$2"
  if unit_active "${unit}"; then
    printf -v "${state_var}" '1'
    sudo systemctl stop "${unit}"
  fi
}

restore_services() {
  local restore_rc=0
  set +e
  if [[ "${bridge_was_active}" == "1" ]]; then
    # The bridge intentionally carries StartLimitAction=reboot. Clear the
    # operator probe's stop/start history before restoring it.
    sudo systemctl reset-failed jasper-aec-bridge.service || restore_rc=1
    sudo systemctl start jasper-aec-bridge.service || restore_rc=1
  fi
  if [[ "${voice_was_active}" == "1" ]]; then
    sudo systemctl start jasper-voice.service || restore_rc=1
  fi
  if [[ "${nqptp_was_active}" == "1" ]]; then
    sudo systemctl restart nqptp.service || restore_rc=1
  fi
  if [[ "${shairport_was_active}" == "1" ]]; then
    # A restart clears the half-open AP2 session left by stopping shairport.
    sudo systemctl restart shairport-sync.service || restore_rc=1
  fi
  return "${restore_rc}"
}

on_exit() {
  local rc=$?
  restore_services
  local restore_rc=$?
  if [[ "${rc}" -eq 0 ]]; then
    exit "${restore_rc}"
  fi
  exit "${rc}"
}

install_aec_probe_service_guard() {
  trap on_exit EXIT
}
