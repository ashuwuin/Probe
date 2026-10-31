// Sample Rust used by the regex fallback tests.
pub struct Ledger {
    entries: Vec<i64>,
}

pub fn post_entry(ledger: &mut Ledger, amount: i64) {
    ledger.entries.push(amount);
}
