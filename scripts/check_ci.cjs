"use strict";

const required = ["frontend", "backend", "automation", "security"];

function check(results) {
  if (!results || typeof results !== "object" || Array.isArray(results)) return false;
  return Object.keys(results).length === required.length &&
    required.every((job) => results[job]?.result === "success");
}

if (require.main === module) {
  let passed = false;
  try { passed = check(JSON.parse(process.env.CI_NEEDS || "null")); } catch { /* fail closed */ }
  if (!passed) {
    console.error("CI requires successful Frontend, Backend, Automation and Security jobs.");
    process.exitCode = 1;
  }
}

module.exports = { check, required };
