// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Run a small dependency-free Node test list and emit the JSON verdict used by
 * the pytest wrappers. Test functions may be synchronous or asynchronous.
 */
export async function runTestFunctions(tests, passedCount) {
  for (const test of tests) {
    try {
      await test();
    } catch (error) {
      const failure = {
        test: test.name,
        error: String(error && error.stack ? error.stack : error),
      };
      console.error(failure.error);
      console.log(JSON.stringify({ ok: false, ...failure }));
      process.exit(1);
    }
  }

  console.log(JSON.stringify({ ok: true, passed: passedCount() }));
}
