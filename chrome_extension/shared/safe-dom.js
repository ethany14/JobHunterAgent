"use strict";

export function clearNode(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

export function textElement(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = value == null ? "" : String(value);
  return node;
}

export function button(label, onClick, className = "secondary-button") {
  const node = textElement("button", className, label);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}
