/** Latest-request-wins fencing for overlapping private Xpra inventory polls.
 * No request/response identifier is treated as an authorization capability.
 */
export class AgentXpraInventoryGuard {
  private generation = 0;
  private stopped = false;

  begin(): number {
    if (this.stopped || this.generation >= Number.MAX_SAFE_INTEGER) return 0;
    return ++this.generation;
  }

  accepts(generation: number): boolean {
    return !this.stopped && generation > 0 && generation === this.generation;
  }

  stop(): void { this.stopped = true; this.generation++; }
}
