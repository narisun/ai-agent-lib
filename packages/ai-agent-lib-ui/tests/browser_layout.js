/* Read-only browser regression check for New chat and reopened empty threads.
   Run this function in the browser's page context after each layout change.
   Cover 375, 800 and 1305 px widths, sidebar open/closed, and Memos open/closed.
   The check walks the real welcome ancestors, including Chainlit's nested
   scrollers; checking document.scrollWidth alone misses the reported bug. */
(() => {
  const welcome = document.querySelector("#welcome-screen");
  if (!welcome) throw new Error("Open New chat or a saved empty thread first.");
  const ancestors = [];
  for (let element = welcome; element; element = element.parentElement) {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    ancestors.push({
      element: element.id || element.tagName.toLowerCase(),
      overflowX: style.overflowX,
      overflowY: style.overflowY,
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      left: rect.left,
      right: rect.right,
    });
  }
  const horizontal = ancestors.filter(element =>
    element.overflowX === "scroll" ||
    (element.overflowX === "auto" && element.scrollWidth > element.clientWidth)
  );
  if (horizontal.length) {
    throw new Error("Unexpected horizontal scrollbar: " + JSON.stringify(horizontal));
  }
  for (const selector of ["#welcome-screen h2", "#message-composer", "#chat-input"]) {
    const element = document.querySelector(selector);
    if (!element) throw new Error("Missing welcome control: " + selector);
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.left < -1 || rect.right > innerWidth + 1) {
      throw new Error("Welcome control extends past viewport: " + selector);
    }
  }
  return {width: innerWidth, height: innerHeight, ancestors};
})()
