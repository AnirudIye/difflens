function summarize(rows) {
  const amounts = rows.map((row) => row.amount);
  return amounts.reduce((running, amount) => running + amount, initialValue);
}

module.exports = { summarize };
