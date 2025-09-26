#  Railway QR Vendor Portal

This repository contains the **Vendor Site** for the Railway QR Project.  
Vendors can register, manage their fittings, generate **AI-based QR codes**, and transmit G-codes to an ESP32 for engraving/printing.

---

##  Screenshots

- Vendor Registration Page

   ![WhatsApp Image 2025-09-13 at 00 34 31_58da419a](https://github.com/user-attachments/assets/5743408b-a9a0-4537-880c-39ed4c4a360d)


- Vendor Login Page
  
  ![WhatsApp Image 2025-09-13 at 00 34 30_f7e37468](https://github.com/user-attachments/assets/d3a1a907-f2aa-4561-a66b-0da18f06ab3e)


- Vendor Dashboard
  
![WhatsApp Image 2025-09-13 at 00 34 30_8eeef0c4](https://github.com/user-attachments/assets/c446ca81-f571-4d02-956a-c11d0e1f9214)

- Data Entry Form
  
  ![WhatsApp Image 2025-09-12 at 23 18 44_eb3149f6](https://github.com/user-attachments/assets/b079ed56-b5a8-46a1-8201-087d8f61121a)

- QR Code Generation
  
![WhatsApp Image 2025-09-12 at 19 58 01_5a1e9517](https://github.com/user-attachments/assets/537d97a0-322b-4481-91aa-b7044cf03440)

- QR code

  ![WhatsApp Image 2025-09-12 at 19 58 22_01629831](https://github.com/user-attachments/assets/2310b4aa-ea41-48ee-9b57-2114b3295400)
  

- QR code engraving simulation
  
  ![WhatsApp Image 2025-09-12 at 20 07 25_105e9d7b](https://github.com/user-attachments/assets/b5b68b28-7f2a-4451-8e7c-955e44f9a59f)
  

- Hardware (esp32) -Sofware pipeline

   ![WhatsApp Image 2025-09-13 at 00 59 14_17ac0202](https://github.com/user-attachments/assets/175b9fa5-0be6-4daa-aa62-13a00f5d84a9)

---

##  Features

1. **Vendor Registration/Login**  
   - Secure authentication system for vendors.  
   - Each vendor has a personalized dashboard.  

2. **Vendor Dashboard & QR Management**  
   - Vendors can view their existing fittings.  
   - Each vendor receives a unique QR for identification.  

3. **Data Entry for Fittings**  
   - Enter details like part name, installation date, location, etc.  
   - Data stored in the backend for future inspections & tracking.  

4. **AI-based QR Code Generation**  
   - Automatically generate unique QR codes for each fitting.  
   - Codes are linked to the vendor database.  

5. **Convert QR to G-codes & Transmit to ESP32**  
   - QR codes are translated into **G-code instructions**.  
   - G-codes are transmitted to an **ESP32-based engraver/printer** for physical marking.
  
6. **AI-based risk assessment**
   - This website assess the risk level of the fitting based on several factor

7. **Vendor Risk assessment**
   - It assess the vendor risk based on the risk level of each products that a particular vendor ships

8. **AI-based inspection dates generation**
   - Generates an inspection date for each fitting based on it's risk level

---

##  Tech Stack

- **Frontend:** HTML, CSS, JavaScript  
- **Backend:** Flask 
- **Database:** SQLite 
- **QR Generation:** Python QR libraries / AI-based QR styling  
- **Hardware Communication:** Wi-Fi transfer of generated G-codes to ESP32  

---

##  Getting Started

### Prerequisites
- Python 3.10+  
- ESP32 with Wi-Fi enabled firmware  
- Engraver/Printer hardware connected to ESP32  

### Installation
```bash
# Clone the repository
git clone https://github.com/Debanshu2005/railway-qr-vendor.git
cd railway-qr-vendor

# Install dependencies
pip install -r requirements.txt   
```

## Run the Vendor Site
  ```bash
# Start server
python app.py
```
## Access

- Open http://localhost:5000 

- Register/login as a vendor.

---

## ESP32 Integration

- Once a QR code is generated, it is converted into G-code paths.

- The G-code is sent to the ESP32 over Wi-Fi/serial for engraving/printing.

- The ESP32 executes the engraving on the selected material.

---

## License

MIT License © 2025 Debanshu2005
