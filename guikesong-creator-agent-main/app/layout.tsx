import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: '贵客松 · 贵州文旅创作智能体',
  description: '为贵州文旅博主建立会生长的内容资产与创作闭环。',
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
