(function () {
  var SRC = "https://thirty-days-ai.onrender.com/chashmal?embed=1";
  var root = document.getElementById("am-chat");
  if (!root) return;

  var toggle = root.querySelector(".am-chat-toggle");
  var panel = document.getElementById("am-chat-panel");
  var closer = root.querySelector(".am-chat-close");
  var frame = null;

  function isOpen() {
    return root.classList.contains("is-open");
  }

  function ensureFrame() {
    if (frame) return;
    frame = document.createElement("iframe");
    frame.src = SRC;
    frame.title = "סוכן מענה לשרטוט חשמל";
    frame.setAttribute("allow", "clipboard-write");
    panel.appendChild(frame);
  }

  function open() {
    ensureFrame();
    root.classList.add("is-open");
    if (toggle) toggle.setAttribute("aria-expanded", "true");
    if (panel) panel.hidden = false;
  }

  function close() {
    root.classList.remove("is-open");
    if (toggle) toggle.setAttribute("aria-expanded", "false");
    if (panel) panel.hidden = true;
  }

  function togglePanel() {
    if (isOpen()) close();
    else open();
  }

  if (toggle) toggle.addEventListener("click", togglePanel);
  if (closer) closer.addEventListener("click", close);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && isOpen()) close();
  });
})();
