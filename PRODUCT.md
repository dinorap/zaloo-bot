# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Flask + Jinja2 templates, vanilla JS, phục vụ trên máy Windows đóng gói bằng PyInstaller thành một file EXE. Không có build step cho CSS/JS.

## Users

Người bán hàng / chủ shop dùng Zalo làm kênh CSKH chính. Họ vừa trả lời khách trên điện thoại vừa cần bot tự trả lời các câu hỏi lặp lại (giá, tồn kho, chính sách đổi trả). Họ không rành kỹ thuật: tự tạo bot, lấy token, nhập chat ID là những việc mà giao diện phải hướng dẫn từng bước.

## Product Purpose

Jimmy BOT CHAT là bảng điều khiển vận hành Zalo CSKH: quản lý tài khoản Zalo đã đăng nhập, hội nhóm, tin nhắn hội thư, chiến dịch gửi tin hàng loạt, lịch hoạt động, và chatbot trả lời tự động bằng AI.

Thành công = người dùng cài app, đăng nhập Zalo, bật chatbot và tin nhắn khách được trả lời đúng theo tài liệu họ dán vào, mà không cần nhờ ai can thiệp kỹ thuật.

## Positioning

Một sản phẩm duy nhất gộp: đăng nhập & vận hành nhiều tài khoản Zalo + chatbot AI + chiến dịch gửi tin, chạy offline trên máy người dùng, dữ liệu không rời máy. Cạnh tranh bằng "một app cho người bán hàng", không phải bằng chatbot đơn lẻ.

## Operating Context

- Chạy trên một máy Windows của chủ shop, thường chạy song song trình duyệt Edge profile để giữ phiên đăng nhập Zalo.
- Quản lý tài khoản Zalo nhiều profile, mỗi profile một tài khoản.
- Chatbot chạy nền trong chính tiến trình app, tự poll tin nhắn mới trong nhóm và trả lời.
- AI dùng Gemini API Key do người dùng tự mua, có thể xoay nhiều key.
- Người dùng dán tài liệu sản phẩm/bảng giá vào một ô textarea và bot ưu tiên trả lời theo tài liệu đó.

## Capabilities and Constraints

- Chatbot Zalo: chọn tài khoản + nhóm, chế độ echo/gemini, trigger keyword, nhiều API key xoay vòng, RAG tài liệu, chống loop, tự trả lời tại nhóm nguồn.
- Chatbot Telegram: Bot API token + chat ID, cùng bộ não Gemini/RAG dùng chung với Zalo.
- Chiến dịch: gửi tin nhóm, sao chép nhóm, lịch hoạt động theo lịch.
- Quản lý: tài khoản, nhóm, thành viên, bạn bè, hộp thư chung, gateway đa máy.
- Ràng buộc: Windows + PyInstaller; một tiến trình; dữ liệu lưu JSON trong `data/`; không có auth web (ứng dụng chạy cục bộ).
- Ràng buộc Telegram: dùng Bot API long polling, khách không cần mở port, không cần HTTPS.

## Brand Commitments

- Tên sản phẩm: **Jimmy BOT CHAT** (thay thế "Nexus" trước đây).
- Bảng màu cam-vàng do người dùng chốt: cam chủ đạo `#ff7542`, vàng kim `#f5b301`, nền kem/trắng ấm.
- Ngôn ngữ giao diện: tiếng Việt.
- Giọng nói: thân thiện, hướng dẫn từng bước, không dùng từ kỹ thuật khi không cần.

## Evidence on Hand

- Sản phẩm chạy được: `_ref_mkt_zalo/app.py` (Flask), 25+ trang templates.
- Ảnh tham chiếu màu: cam `#ff7542` trích từ ảnh người dùng gửi.
- Không có testimonial, benchmark, giá, hay tuyên bố thương mại nào — không được bịa.

## Product Principles

1. Khách không rành kỹ thuật vẫn tự làm xong một việc mà không cần hỏi ai. Mỗi trang mới phải tự giải thích từng bước.
2. Một nơi duy nhất cho trí nhớ dài hạn: tài liệu, nhân viên, và câu trả lời mẫu dùng chung giữa Zalo và Telegram.
3. Trạng thái phải nói thẳng: bot đang chạy hay dừng, key còn quota hay hết, đã lưu hay chưa — không để người dùng đoán.
4. Nút bấm phải cho kết quả ngay và cho biết kết quả đó.
5. Không mất dữ liệu người dùng khi cập nhật giao diện: cấu hình và tài liệu tách khỏi code giao diện.

## Accessibility & Inclusion

Đối tượng là người bán hàng, thường dùng điện thoại nhỏ. Bố cục phải dùng được ở 390px. Tương phản chữ ≥4.5:1. Nhãn tiếng Việt rõ ràng, trạng thái không chỉ truyền qua màu.
