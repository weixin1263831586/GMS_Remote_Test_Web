// GFM 表格单元格内的 \| 是字面管道符，不是列分隔符。
function _splitMarkdownTableRow(row) {
  var guard = '\u0000';
  var cells = String(row).split('\\|').join(guard).split('|').map(function (cell) {
    return cell.split(guard).join('|').trim();
  });
  if (cells.length && cells[0] === '') cells.shift();
  if (cells.length && cells[cells.length - 1] === '') cells.pop();
  return cells;
}
