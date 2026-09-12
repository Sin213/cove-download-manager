// extension/confirm-exclude.js

const browser = globalThis.browser || globalThis.chrome;
const token = location.hash.slice(1);
const question = document.getElementById("question");
const status = document.getElementById("status");
const cancelButton = document.getElementById("cancel");
const confirmButton = document.getElementById("confirm");

function finish(message) {
  status.textContent = message;
  cancelButton.disabled = true;
  confirmButton.disabled = true;
}

function fail() {
  finish("This confirmation is no longer available. Start again from the Cove button.");
}

async function loadConfirmation() {
  if (!token) {
    fail();
    return;
  }
  try {
    const reply = await browser.runtime.sendMessage({
      type: "getExcludeConfirmation", token,
    });
    if (!reply || reply.ok !== true || typeof reply.host !== "string") {
      fail();
      return;
    }
    question.textContent = "Exclude " + reply.host + " from Cove?";
    status.textContent = "Review the site, then confirm or cancel.";
    confirmButton.disabled = false;
  } catch {
    fail();
  }
}

cancelButton.addEventListener("click", async () => {
  cancelButton.disabled = true;
  confirmButton.disabled = true;
  try {
    const reply = await browser.runtime.sendMessage({
      type: "cancelExcludeConfirmation", token,
    });
    status.textContent = reply && reply.ok === true
      ? "Cancelled. No settings were changed."
      : "This confirmation is no longer available.";
  } catch {
    status.textContent = "Could not cancel this confirmation.";
  }
});

confirmButton.addEventListener("click", async () => {
  cancelButton.disabled = true;
  confirmButton.disabled = true;
  status.textContent = "Excluding site…";
  try {
    const reply = await browser.runtime.sendMessage({
      type: "confirmExcludeSite", token,
    });
    if (!reply || reply.ok !== true) {
      status.textContent = "Could not exclude this site. Start again from the Cove button.";
      return;
    }
    status.textContent = reply.alreadyExcluded
      ? "This site is already excluded."
      : "Site excluded from Cove.";
  } catch {
    status.textContent = "Could not exclude this site. Start again from the Cove button.";
  }
});

loadConfirmation();
