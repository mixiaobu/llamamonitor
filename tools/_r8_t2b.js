(function(){
  // 接续上一连接状态不可用（fresh connection），这里重新走一遍完整协议：
  // 但 __tset 只在当前 document 存在。fresh navigate 会重置，所以本脚本独立：
  // 预热 -> 基线 -> 40 次 -> 再 40 次 -> 对比两轮 delta 是否相当（相当=收敛，递增=泄漏）
  return 'see py';
})()
