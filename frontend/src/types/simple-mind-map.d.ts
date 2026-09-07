// simple-mind-map 未提供 TypeScript 类型声明，这里做模块声明
declare module 'simple-mind-map' {
  const MindMap: any
  export default MindMap
}

declare module 'simple-mind-map/src/plugins/Drag.js' {
  const Drag: any
  export default Drag
}

declare module 'simple-mind-map/src/plugins/Select.js' {
  const Select: any
  export default Select
}
