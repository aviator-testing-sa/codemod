document.addEventListener('DOMContentLoaded', function() {
  const contactForm = document.getElementById('contactform');

  contactForm.addEventListener('submit', function(event) {
    event.preventDefault();

    const action = this.getAttribute('action');
    const messageDiv = document.getElementById('message');
    const submitButton = document.getElementById('submit');

    // Slide up message (hide)
    messageDiv.style.display = 'none'; // Hides immediately, no animation in plain JS

    // Add loading indicator and disable submit button
    const loaderImg = document.createElement('img');
    loaderImg.src = 'images/loading.gif';
    loaderImg.className = 'loader';
    submitButton.insertAdjacentElement('afterend', loaderImg);
    submitButton.disabled = true;

    // Prepare form data
    const formData = new FormData();
    formData.append('name', document.getElementById('name').value);
    formData.append('email', document.getElementById('email').value);
    formData.append('comments', document.getElementById('comments').value);
    formData.append('verify', document.getElementById('verify').value);

    // Send POST request using fetch
    fetch(action, {
      method: 'POST',
      body: formData,
    })
    .then(response => response.text())
    .then(data => {
      // Update message
      messageDiv.innerHTML = data;
      messageDiv.style.display = 'block'; // Slide down effect would require more complex JS

      // Remove loading indicator and enable submit button
      loaderImg.remove();
      submitButton.disabled = false;

      if (data.includes('success')) {
        contactForm.style.display = 'none'; // Slide up effect would require more complex JS
      }
    })
    .catch(error => {
      console.error('Error:', error);
      messageDiv.innerHTML = 'An error occurred. Please try again later.';
      messageDiv.style.display = 'block';

      loaderImg.remove();
      submitButton.disabled = false;
    });
  });
});
