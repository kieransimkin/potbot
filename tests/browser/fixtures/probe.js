"use strict";
document.getElementById("save").addEventListener("click", () => {
  const count = document.getElementById("count");
  count.textContent = String(Number(count.textContent) + 1);
  document.body.classList.add("saved");
});
