import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'RK Test Hub · 自动化测试平台',
  description: '为 RK 产品配置固件来源、测试方案与用例，执行测试并查看可追溯的报告',
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
