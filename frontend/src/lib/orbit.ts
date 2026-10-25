/** Pure positioning and state functions; render changes through GPU transforms only. */
export type OrbPosition = { x: number; y: number; scale: number; opacity: number; z: number };

export function relativeOrbitIndex(index: number, active: number, count: number): number {
  if (count <= 0) return 0;
  let delta = ((index - active) % count + count) % count;
  if (delta > count / 2) delta -= count;
  return delta;
}

export function orbPosition(index: number, active: number, count: number, revealed: boolean): OrbPosition {
  const step = relativeOrbitIndex(index, active, count);
  if (step === 0) return { x: 0, y: 0, scale: 1, opacity: 1, z: 12 };
  if (!revealed) return { x: Math.sign(step) * Math.min(Math.abs(step) * 75, 250), y: -185, scale: .65, opacity: 0, z: 0 };
  const angle = Math.min(Math.abs(step) * .45, 1.44);
  return { x: Math.round(Math.sin(angle) * 150) * Math.sign(step), y: -Math.round((1 - Math.cos(angle)) * 120) - 26, scale: Math.max(.68, 1 - Math.abs(step) * .09), opacity: Math.max(.38, 1 - Math.abs(step) * .16), z: 10 - Math.abs(step) };
}

export function nextWorkspace(index: number, direction: -1 | 1, count: number): number {
  return count ? (index + direction + count) % count : 0;
}
