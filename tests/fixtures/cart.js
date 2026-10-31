// Sample JavaScript used by the regex fallback tests.
function addItem(cart, item) {
  cart.push(item);
}

const removeItem = (cart, id) => cart.filter((i) => i.id !== id);

class Cart {
  checkout() {
    return true;
  }
}
