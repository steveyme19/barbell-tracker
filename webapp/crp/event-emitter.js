/*
 * Minimal browser EventEmitter — replaces Node 'events' module.
 */

export class EventEmitter {
  constructor() {
    this._listeners = new Map();
  }

  on(event, fn) {
    let list = this._listeners.get(event);
    if (!list) {
      list = [];
      this._listeners.set(event, list);
    }
    list.push(fn);
    return this;
  }

  off(event, fn) {
    const list = this._listeners.get(event);
    if (!list) return this;
    const idx = list.indexOf(fn);
    if (idx !== -1) list.splice(idx, 1);
    return this;
  }

  once(event, fn) {
    const wrapper = (...args) => {
      this.off(event, wrapper);
      fn(...args);
    };
    return this.on(event, wrapper);
  }

  emit(event, ...args) {
    const list = this._listeners.get(event);
    if (!list) return false;
    for (const fn of [...list]) fn(...args);
    return true;
  }

  removeListener(event, fn) {
    return this.off(event, fn);
  }
}
