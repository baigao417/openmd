// Synthetic metadata; this adapter is never executed.
cli({
  site: 'example', name: 'detail', access: 'read',
  args: [{name: 'id', positional: true}, {name: 'markdown', type: 'boolean'}],
  columns: ['Role', 'Text'],
  func: async () => []
});
