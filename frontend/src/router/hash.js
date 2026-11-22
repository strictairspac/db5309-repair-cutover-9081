export function route() {
  const [key, id, mode] = (location.hash.slice(1) || 'cases').split('/');
  return {key, id:Number(id) || 0, mode: mode || ''};
}
export function go(to) {
  location.hash = to;
}
