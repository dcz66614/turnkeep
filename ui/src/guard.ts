// Render generation guard (T032 fix): hash changes while a view is still
// fetching must not let the older fetch overwrite the newer view.
// Each render() takes a generation; views check isCurrent(gen) after every
// await before writing innerHTML.
let gen = 0;

export function nextGen(): number {
  gen += 1;
  return gen;
}

export function isCurrent(g: number): boolean {
  return g === gen;
}
