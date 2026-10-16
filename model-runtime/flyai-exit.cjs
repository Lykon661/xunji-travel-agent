'use strict';

// Loaded only in FlyAI child processes: let Windows fetch handles and output drain.
// https://github.com/nodejs/node/issues/56645
const requestedExit = new Error('FlyAI CLI requested exit');

process.exit = (code) => {
  process.exitCode = code ?? process.exitCode ?? 0;
  // Returning would run the CLI's failure branch after a successful response.
  throw requestedExit;
};

function finish(reason) {
  if (reason === requestedExit) return;
  process.exitCode = 1;
  console.error(reason);
}

process.on('unhandledRejection', finish);
process.on('uncaughtException', finish);
