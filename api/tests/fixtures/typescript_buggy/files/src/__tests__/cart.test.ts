import { total } from "../cart";

test("total sums the lines", () => {
  expect(total([{ sku: "a", quantity: 2, price: 3 }])).toBe(6);
});
