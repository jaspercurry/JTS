// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

/** Run one optimistic scan toggle while keeping failure recovery testable. */
export async function toggleScanRequest({
  discovering,
  setIntentUntil,
  render,
  postScan,
  refreshState,
  showAlert,
  now = Date.now,
  defer = setTimeout,
}) {
  const action = discovering ? "stop" : "start";
  setIntentUntil(action === "start" ? now() + 3000 : 0);
  render();
  try {
    await postScan(action);
  } catch (error) {
    setIntentUntil(0);
    render();
    const message = error && error.status
      ? "Bluetooth scan failed: " + (error.message || "HTTP " + error.status)
      : "Network error talking to the Bluetooth backend.";
    await showAlert(message);
    await refreshState();
    return false;
  }
  defer(refreshState, 200);
  return true;
}
