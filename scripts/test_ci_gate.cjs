"use strict";

const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const { check, required } = require("./check_ci.cjs");

const success = () => Object.fromEntries(required.map((job) => [job, { result: "success" }]));
assert.equal(check(success()), true);
for (const job of required) {
  for (const result of ["failure", "cancelled", "skipped", "pending", "", null, undefined]) {
    const results = success();
    results[job] = { result };
    assert.equal(check(results), false, `${job}: ${result} must block CI`);
  }
  const results = success();
  delete results[job];
  assert.equal(check(results), false, `${job}: missing must block CI`);
}
for (const value of [null, [], {}, "success", { ...success(), unknown: { result: "success" } }]) {
  assert.equal(check(value), false);
}
for (const input of ["", "invalid JSON", "null", JSON.stringify(success())]) {
  const result = spawnSync(process.execPath, [require.resolve("./check_ci.cjs")], {
    env: { ...process.env, CI_NEEDS: input }, encoding: "utf8",
  });
  assert.equal(result.status, input === JSON.stringify(success()) ? 0 : 1);
}
console.log("CI gate rejects failed, cancelled, skipped, missing and malformed results.");
