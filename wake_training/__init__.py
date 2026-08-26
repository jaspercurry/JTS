# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Offline wake-word training helpers.

This package owns reusable data-prep contracts for the custom wake-word
training workflow. It must stay side-effect-light: importing it should not
load models, touch audio hardware, or mutate Pi runtime state.
"""
